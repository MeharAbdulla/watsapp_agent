import os
import time
import random
import glob
from datetime import datetime
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import WebDriverException, TimeoutException
from webdriver_manager.chrome import ChromeDriverManager

# Import native local PDF parser library
from pypdf import PdfReader

import gemini_chat
import local_history
from agent_config import get_typing_profile, load_tenant_agent_config
from conversation_flow import ConversationFlow
from tenant_paths import ensure_tenant_layout

# Load environment configuration
load_dotenv(".env")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PDF_MAX_CHARS = int(os.getenv("PDF_MAX_CHARS", "12000"))


class PdfExtractor:
    """Extracts text from PDF files and persists results for the agent."""

    def __init__(self, download_dir, extracted_dir, log_fn, max_chars=PDF_MAX_CHARS, tenant_id="default"):
        self.download_dir = download_dir
        self.extracted_dir = extracted_dir
        self.log = log_fn
        self.tenant_id = tenant_id
        self.max_chars = max_chars
        os.makedirs(self.extracted_dir, exist_ok=True)

    def _list_pdfs(self):
        return glob.glob(os.path.join(self.download_dir, "*.pdf"))

    def wait_for_new_pdf(self, known_paths, timeout=45):
        """Poll until a PDF appears that was not in known_paths."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            current = set(self._list_pdfs())
            new_files = [p for p in current if p not in known_paths]
            if new_files:
                return max(new_files, key=os.path.getctime)
            time.sleep(0.5)
        return None

    def extract_from_file(self, pdf_path, label="document"):
        """Read all pages, trim for API context, and save a .txt copy."""
        filename = os.path.basename(pdf_path)
        self.log("PDF", f"Extracting: {filename}")

        try:
            reader = PdfReader(pdf_path)
            page_count = len(reader.pages)
            parts = []
            for i, page in enumerate(reader.pages, start=1):
                block = page.extract_text()
                if block:
                    parts.append(block.strip())

            full_text = "\n\n".join(parts).strip()
            if not full_text:
                return {
                    "ok": False,
                    "filename": filename,
                    "pages": page_count,
                    "text": None,
                    "saved_path": None,
                    "error": "No extractable text (scanned/image-only PDF).",
                }

            truncated = len(full_text) > self.max_chars
            text_for_agent = full_text[: self.max_chars]
            if truncated:
                text_for_agent += f"\n\n[... truncated at {self.max_chars} characters ...]"

            saved_path = None
            try:
                from firebase_store import is_active, save_extracted_document

                if is_active():
                    doc_id = save_extracted_document(
                        self.tenant_id,
                        filename,
                        full_text,
                        pages=page_count,
                        label=label,
                    )
                    saved_path = f"firebase:{doc_id}"
                    self.log(
                        "PDF",
                        f"Done — {page_count} page(s), {len(full_text)} chars → Firebase ({doc_id})",
                    )
            except Exception as fb_err:
                self.log("PDF_WARN", f"Firebase save failed, using local file: {fb_err}")

            if not saved_path:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
                txt_name = f"{safe_label}_{stamp}.txt"
                saved_path = os.path.join(self.extracted_dir, txt_name)
                with open(saved_path, "w", encoding="utf-8") as out:
                    out.write(f"Source: {filename}\nPages: {page_count}\n")
                    out.write("=" * 50 + "\n\n")
                    out.write(full_text)
                self.log(
                    "PDF",
                    f"Done — {page_count} page(s), {len(full_text)} chars → {txt_name}",
                )
            return {
                "ok": True,
                "filename": filename,
                "pages": page_count,
                "char_count": len(full_text),
                "full_text": full_text,
                "text": text_for_agent,
                "saved_path": saved_path,
                "truncated": truncated,
                "error": None,
            }
        except Exception as err:
            self.log("PDF_ERROR", f"Extraction failed for {filename}: {err}")
            return {
                "ok": False,
                "filename": filename,
                "pages": 0,
                "text": None,
                "saved_path": None,
                "error": str(err),
            }

    def extract_latest_download(self, label="document"):
        """Wait for a new download, extract text, optionally remove the PDF."""
        known = set(self._list_pdfs())
        pdf_path = self.wait_for_new_pdf(known)
        if not pdf_path:
            self.log("PDF_ERROR", "Download timed out — no PDF found in downloads folder.")
            return None
        result = self.extract_from_file(pdf_path, label=label)
        try:
            os.remove(pdf_path)
        except OSError:
            pass
        return result

    def build_agent_message(self, result, user_caption=""):
        if not result or not result.get("ok"):
            err = (result or {}).get("error", "Unknown error")
            return (
                "The client sent a PDF attachment but text could not be extracted. "
                f"Reason: {err}. Reply politely and ask them to resend a text-based PDF or describe their request."
            )
        header = (
            f"[PDF extracted — file: {result['filename']}, "
            f"{result['pages']} page(s), {result['char_count']} characters"
        )
        if result.get("saved_path"):
            header += f", saved: {os.path.basename(result['saved_path'])}"
        header += "]"
        caption = user_caption.strip()
        body = result["text"]
        if caption:
            return f"{header}\n\nClient message with PDF:\n{caption}\n\n--- PDF content ---\n{body}"
        return f"{header}\n\n--- PDF content ---\n{body}\n\nReview the document and respond to the client appropriately."


class WhatsAppBusinessBot:
    def __init__(self, tenant_id="default", on_event=None, managed=False, headless=False):
        self.tenant_id = tenant_id
        self.on_event = on_event
        self.managed = managed
        self.headless = headless
        self.running = True
        self.processed_messages = {}
        self.driver = None
        self.wait = None

        if tenant_id == "default":
            tenant_root = BASE_DIR
            self.download_dir = os.path.join(BASE_DIR, "downloads")
            self.extracted_dir = os.path.join(BASE_DIR, "extracted")
            self.chrome_data_dir = os.path.join(BASE_DIR, "chrome-data")
            flow_base = BASE_DIR
        else:
            paths = ensure_tenant_layout(tenant_id)
            tenant_root = paths["root"]
            self.download_dir = paths["downloads"]
            self.extracted_dir = paths["extracted"]
            self.chrome_data_dir = paths["chrome_data"]
            flow_base = paths["root"]

        os.makedirs(self.download_dir, exist_ok=True)
        os.makedirs(self.chrome_data_dir, exist_ok=True)

        try:
            from firebase_store import firebase_required, init_firebase, use_firebase

            if firebase_required() and use_firebase():
                init_firebase()
                self.log("FIREBASE", "Connected — using Firestore for state and PDF text")
            elif firebase_required():
                self.log("FIREBASE_WARN", "Set FIREBASE_CREDENTIALS in .env for cloud storage")
        except Exception as err:
            self.log("FIREBASE_WARN", str(err))

        self.pdf_extractor = PdfExtractor(
            self.download_dir, self.extracted_dir, self.log, tenant_id=tenant_id
        )
        self.agent_config = load_tenant_agent_config(tenant_id)
        self._apply_typing_profile()
        self.flow = ConversationFlow(flow_base, self.log, tenant_id=tenant_id)

    def reload_agent_config(self):
        self.agent_config = load_tenant_agent_config(self.tenant_id)
        self._apply_typing_profile()
        if hasattr(self, "flow"):
            self.flow.reload_config()

    def _apply_typing_profile(self):
        profile = get_typing_profile(self.agent_config.get("typing_profile", "slow"))
        self._typing = profile

    def _emit(self, event: str, data: dict):
        if self.on_event:
            try:
                self.on_event(event, data)
            except Exception:
                pass

    def log(self, level, message):
        """Standardized professional console logging."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] [{level}] {message}")
        self._emit("log", {"level": level, "message": message})

    def initialize_webdriver(self, wait_for_login=True):
        """Initializes Chrome for WhatsApp Web (per-tenant profile)."""
        self.log("INIT", "Launching Chrome for WhatsApp Web...")
        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
            options.add_argument("--window-size=1280,900")
        else:
            options.add_argument("--start-maximized")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-sandbox")
        options.add_argument(f"--user-data-dir={self.chrome_data_dir}")
        if not self.managed:
            options.add_experimental_option("detach", True)

        prefs = {
            "download.default_directory": self.download_dir,
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "plugins.always_open_pdf_externally": True,
        }
        options.add_experimental_option("prefs", prefs)

        try:
            self.driver = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()),
                options=options,
            )
            self.wait = WebDriverWait(self.driver, 10)
            self.driver.get("https://web.whatsapp.com")
            self._emit("status", {"status": "qr_pending"})

            if wait_for_login and not self.managed:
                self.log("INIT", "Scan QR code if shown. Waiting up to 20s...")
                deadline = time.time() + 20
                while time.time() < deadline and not self.is_logged_in():
                    time.sleep(2)
                if self.is_logged_in():
                    self.log("INIT", "WhatsApp connected.")
                    self._emit("status", {"status": "connected"})
                else:
                    self.log("INIT", "Waiting for QR scan...")
        except Exception as e:
            self.log("CRITICAL", f"Failed to initialize browser: {e}")
            self._emit("status", {"status": "error", "message": str(e)})
            raise e

    def is_logged_in(self) -> bool:
        if not self.driver:
            return False
        try:
            selectors = [
                "#pane-side",
                'div[id="pane-side"]',
                '[data-testid="chat-list"]',
                'div[data-testid="chat-list"]',
            ]
            for selector in selectors:
                if self.driver.find_elements(By.CSS_SELECTOR, selector):
                    return True
            return False
        except WebDriverException:
            return False

    def get_qr_base64(self) -> str | None:
        if not self.driver or self.is_logged_in():
            return None
        try:
            canvas = WebDriverWait(self.driver, 3).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'canvas[aria-label*="Scan"], canvas')
                )
            )
            return canvas.screenshot_as_base64
        except Exception:
            try:
                return self.driver.get_screenshot_as_base64()
            except Exception:
                return None

    def ensure_main_chat_list(self):
        """Ensures the bot stays out of submenus and Archive listings entirely."""
        try:
            back_buttons = self.driver.find_elements(By.XPATH, '//button[@aria-label="Back"] | //span[@data-icon="back"]/ancestor::button')
            if back_buttons:
                archive_header = self.driver.find_elements(By.XPATH, '//div[text()="Archived"] | //h1[text()="Archived"] | //span[@title="Archived"]')
                if archive_header:
                    self.log("GUARD", "Detected Archive folder view! Forcefully breaking out to main listing panel...")
                    back_buttons[0].click()
                    time.sleep(1.5)
        except Exception as e:
            self.log("GUARD_ERR", f"Failed routing validation frame checks: {e}")

    def open_chat_by_name(self, chat_name):
        """Finds and clicks a chat in the sidebar, or uses the search bar if not visible."""
        self.ensure_main_chat_list()
        chats = self.driver.find_elements(
            By.XPATH, '//div[@role="listitem"] | //div[contains(@class, "_ak8l")] | //div[@data-testid="cell-frame-container"]'
        )
        for chat in chats:
            try:
                name_el = chat.find_element(By.XPATH, './/span[@dir="auto"] | .//span[contains(@class, "title")]')
                if name_el.text.strip().lower() == chat_name.lower():
                    chat.click()
                    if self.wait_for_chat_panel(timeout=8):
                        time.sleep(1)
                        return True
            except Exception:
                continue

        try:
            self.log("ACTION", f"Chat '{chat_name}' not visible in viewport. Searching...")
            search_xpaths = [
                '//div[@contenteditable="true"][@data-tab="3"]',
                '//div[contains(@class, "lexical-rich-text-input")]//div[@contenteditable="true"]',
                '//div[@data-testid="chat-list-search"]',
                '//label[contains(@class, "_3y9t")]//input'
            ]
            search_box = None
            for xpath in search_xpaths:
                try:
                    el = self.driver.find_element(By.XPATH, xpath)
                    if el.is_displayed():
                        search_box = el
                        break
                except Exception:
                    continue

            if not search_box:
                raise Exception("Search input box not found")

            search_box.click()
            time.sleep(0.5)
            search_box.send_keys(Keys.CONTROL + "a")
            search_box.send_keys(Keys.BACKSPACE)
            time.sleep(0.2)
            search_box.send_keys(chat_name)
            time.sleep(1.5)
            search_box.send_keys(Keys.ENTER)
            time.sleep(1.5)
            
            if self.wait_for_chat_panel(timeout=8):
                return True
            return False
        except Exception as e:
            self.log("ACTION_ERROR", f"Failed to search/open chat '{chat_name}': {e}")
            return False

    def reply_from_flow(self, chat_name, user_query, extra_context=""):
        """Guided order flow or Gemini API based on bot_mode."""
        try:
            bot_mode = self.agent_config.get("bot_mode", "gemini")
            if bot_mode == "flow":
                return self.flow.handle_message(chat_name, user_query, extra_context)
            else:
                history = local_history.load_chat_history(self.tenant_id, chat_name)
                msg_payload = extra_context + "\n\n" + user_query if extra_context else user_query
                return gemini_chat.generate_gemini_reply(chat_name, msg_payload, self.agent_config, history)
        except Exception as e:
            self.log("FLOW_ERROR", f"Bot reply failed: {e}")
            return None

    def remember_exchange(self, chat_name, user_query, ai_reply):
        """Store client message and bot reply in history."""
        if user_query and user_query.strip():
            local_history.ingest_message(self.tenant_id, chat_name, user_query, role="client")
        if ai_reply and ai_reply.strip():
            local_history.ingest_message(self.tenant_id, chat_name, ai_reply, role="assistant")

    def is_row_an_archive_button(self, chat_name):
        """Prevents the bot from accidentally clicking the 'Archived' folder header row item."""
        return "archived" in chat_name.lower()

    def clear_unread_status(self):
        """Forces the current focused room context to drop target focus state to reset badging."""
        try:
            action = webdriver.ActionChains(self.driver)
            action.send_keys(Keys.ESCAPE).perform()
            time.sleep(random.uniform(0.3, 0.6))
        except:
            pass

    def wait_for_chat_panel(self, timeout=15):
        """Wait until the open chat shows messages or a compose box."""
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.find_elements(
                    By.XPATH,
                    '//div[contains(@class, "message-in")] | '
                    '//div[contains(@class, "lexical-rich-text-input")]//div[@contenteditable="true"]',
                )
            )
            return True
        except TimeoutException:
            return False

    def get_incoming_bubbles(self):
        """Collect incoming message bubbles (supports standard and business/service layouts)."""
        selectors = [
            '//div[contains(@class, "message-in")]',
        ]
        seen_keys = set()
        bubbles = []
        for selector in selectors:
            for el in self.driver.find_elements(By.XPATH, selector):
                key = el.id or id(el)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                bubbles.append(el)
        return bubbles

    def extract_incoming_message_text(self, bubble=None):
        """Read text from the last client bubble; tries several WhatsApp Web layouts."""
        targets = [bubble] if bubble else self.get_incoming_bubbles()
        if not targets:
            panel_lines = self.driver.find_elements(
                By.XPATH,
                '//div[@data-testid="conversation-panel-messages"]//span[contains(@class, "selectable-text")] | '
                '//div[@data-testid="conversation-panel-messages"]//div[contains(@class, "copyable-text")]',
            )
            if panel_lines:
                text = (panel_lines[-1].text or "").strip()
                if text:
                    return text
            return ""

        bubble = targets[-1]
        text_xpaths = [
            './/span[contains(@class, "selectable-text")]',
            './/div[contains(@class, "copyable-text")]',
            './/span[@dir="ltr" or @dir="auto"]',
        ]
        for xpath in text_xpaths:
            elements = bubble.find_elements(By.XPATH, xpath)
            for el in reversed(elements):
                text = (el.text or "").strip()
                if text and len(text) > 1:
                    return text

        plain_nodes = bubble.find_elements(By.XPATH, './/*[@data-pre-plain-text]')
        for node in reversed(plain_nodes):
            plain = (node.get_attribute("data-pre-plain-text") or "").strip()
            if plain:
                return plain
        return ""

    def _find_compose_box(self):
        """Locate WhatsApp Lexical compose field."""
        xpaths = [
            '//div[contains(@class, "lexical-rich-text-input")]//div[@contenteditable="true"]',
            '//motion[contains(@class, "lexical-rich-text-input")]//motion[@contenteditable="true"]',
            '//footer//motion[@contenteditable="true"]',
            '//footer//div[@contenteditable="true"]',
            '//motion[@contenteditable="true"][@data-tab="10"]',
            '//div[@contenteditable="true"][@data-tab="10"]',
        ]
        for xpath in xpaths:
            try:
                el = WebDriverWait(self.driver, 4).until(
                    EC.presence_of_element_located((By.XPATH, xpath))
                )
                if el.is_displayed():
                    return el
            except TimeoutException:
                continue
        raise TimeoutException("Compose box not found")

    def _clear_compose_box(self, input_box):
        """Clear Lexical editor without breaking it (do not use innerHTML)."""
        input_box.click()
        time.sleep(0.2)
        action = webdriver.ActionChains(self.driver)
        action.key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).perform()
        time.sleep(0.1)
        action.send_keys(Keys.BACKSPACE).perform()
        time.sleep(0.2)

    def _typing_delay_for_char(self, char: str, words_since_pause: int) -> float:
        profile = getattr(self, "_typing", None) or get_typing_profile("slow")
        delay_min = float(os.getenv("WA_TYPE_DELAY_MIN", profile["delay_min"]))
        delay_max = float(os.getenv("WA_TYPE_DELAY_MAX", profile["delay_max"]))
        punct_delay = float(os.getenv("WA_PUNCT_DELAY", profile["punct_delay"]))

        if char in ".!?":
            delay = random.uniform(punct_delay, punct_delay + 0.5)
        elif char in ",;:":
            delay = random.uniform(0.22, 0.48)
        elif char == " ":
            delay = random.uniform(delay_min + 0.03, delay_max + 0.12)
            if words_since_pause >= random.randint(4, 8):
                delay += random.uniform(0.25, 0.75)
        else:
            delay = random.uniform(delay_min, delay_max)

        if random.random() < 0.03:
            delay += random.uniform(0.45, 1.4)
        return delay

    def _human_type_compose(self, input_box, response_text: str):
        """Type like a human: slow keystrokes, pauses at punctuation and between phrases."""
        text = (response_text or "").replace("\r\n", "\n")
        profile = getattr(self, "_typing", None) or get_typing_profile("slow")
        think_min = float(os.getenv("WA_THINK_MIN", profile["think_min"]))
        think_max = float(os.getenv("WA_THINK_MAX", profile["think_max"]))
        time.sleep(random.uniform(think_min, think_max))

        input_box.click()
        time.sleep(random.uniform(0.15, 0.35))
        words_since_pause = 0

        for char in text:
            if char == "\n":
                input_box.send_keys(Keys.SHIFT, Keys.ENTER)
                time.sleep(random.uniform(0.45, 0.95))
                words_since_pause = 0
                continue

            input_box.send_keys(char)
            if char == " ":
                words_since_pause += 1
                if words_since_pause >= 7:
                    words_since_pause = 0
            time.sleep(self._typing_delay_for_char(char, words_since_pause))
            if char in ".!?":
                words_since_pause = 0

    def _insert_compose_text(self, input_box, text):
        """Insert full message so WhatsApp registers it (paste or insertText)."""
        text = (text or "").strip()
        if not text:
            return False

        try:
            import pyperclip

            pyperclip.copy(text)
            input_box.click()
            time.sleep(0.15)
            webdriver.ActionChains(self.driver).key_down(Keys.CONTROL).send_keys("v").key_up(
                Keys.CONTROL
            ).perform()
            time.sleep(0.4)
            return True
        except Exception:
            pass

        self.driver.execute_script(
            """
            const el = arguments[0];
            const text = arguments[1];
            el.focus();
            document.execCommand('selectAll', false, null);
            document.execCommand('delete', false, null);
            document.execCommand('insertText', false, text);
            el.dispatchEvent(new InputEvent('input', { bubbles: true }));
            """,
            input_box,
            text,
        )
        time.sleep(0.5)
        return True

    def _compose_has_text(self, input_box):
        try:
            body = (input_box.text or "").strip()
            if len(body) > 0:
                return True
            inner = self.driver.execute_script("return arguments[0].innerText || '';", input_box)
            return len((inner or "").strip()) > 0
        except Exception:
            return False

    def _wait_for_send_ready(self, timeout=8):
        """Send button only enables after Lexical has text."""
        xpaths = [
            '//button[@aria-label="Send"]',
            '//button[@data-testid="compose-btn-send"]',
            '//span[@data-icon="send"]/ancestor::button[not(@disabled)]',
        ]
        deadline = time.time() + timeout
        while time.time() < deadline:
            for xpath in xpaths:
                buttons = self.driver.find_elements(By.XPATH, xpath)
                for btn in buttons:
                    try:
                        if btn.is_displayed() and btn.is_enabled():
                            aria = btn.get_attribute("aria-disabled")
                            if aria not in ("true", True):
                                return btn
                    except Exception:
                        continue
            time.sleep(0.25)
        return None

    def _submit_compose(self, input_box):
        """Click Send or press Enter."""
        send_btn = self._wait_for_send_ready(timeout=6)
        if send_btn:
            try:
                send_btn.click()
                time.sleep(0.5)
                return True
            except Exception:
                pass

        input_box.click()
        time.sleep(0.2)
        input_box.send_keys(Keys.ENTER)
        time.sleep(0.5)
        return True

    def send_humanized_reply(self, response_text):
        """Slow human-like typing, then send when WhatsApp enables Send."""
        try:
            input_box = self._find_compose_box()
            self._clear_compose_box(input_box)
            self._human_type_compose(input_box, response_text)

            if not self._compose_has_text(input_box):
                self.log("ACTION", "Typing not detected — using paste fallback.")
                self._clear_compose_box(input_box)
                self._insert_compose_text(input_box, response_text)
                time.sleep(0.5)

            profile = getattr(self, "_typing", None) or get_typing_profile("slow")
            pause = float(os.getenv("WA_PRE_SEND_PAUSE", profile.get("pre_send_pause", 0.9)))
            time.sleep(random.uniform(pause * 0.7, pause * 1.2))
            self._submit_compose(input_box)
            self.log("ACTION", "Humanized reply sent.")
            time.sleep(random.uniform(0.8, 1.4))
            return True
        except Exception as e:
            self.log("ACTION_ERROR", f"Send failed: {e}")
            return False

    def send_business_reply(self, response_text):
        return self.send_humanized_reply(response_text)

    def send_reply(self, response_text):
        return self.send_humanized_reply(response_text)

    def process_chats(self):
        """Scans active viewport lists and processes unread client chat pipelines."""
        if hasattr(self, "flow") and hasattr(self.flow, "reload_states"):
            try:
                self.flow.reload_states()
            except Exception as reload_err:
                self.log("SYSTEM_ERROR", f"Failed reloading states: {reload_err}")
        self.ensure_main_chat_list()

        chats = self.driver.find_elements(
            By.XPATH, '//div[@role="listitem"] | //div[contains(@class, "_ak8l")] | //div[@data-testid="cell-frame-container"]'
        )

        for chat in chats:
            try:
                unread_indicators = chat.find_elements(
                    By.XPATH, 
                    './/span[@aria-label[contains(., "unread")]] | '
                    './/div[contains(@class,"unread")] | '
                    './/span[contains(@class, "_a0km")]'
                )
                if not unread_indicators:
                    continue

                try:
                    chat_name = chat.find_element(By.XPATH, './/span[@dir="auto"] | .//span[contains(@class, "title")]').text.strip()
                except:
                    chat_name = "Unknown Client Contact"

                if self.is_row_an_archive_button(chat_name):
                    continue

                if self.flow.states.get(chat_name, {}).get("paused", False):
                    self.log("SKIP", f"Chat '{chat_name}' is paused (manual CRM takeover active).")
                    continue

                self.log("ALERT", f"Incoming unread request identified: '{chat_name}'")

                time.sleep(random.uniform(0.5, 1.2))
                chat.click()
                if not self.wait_for_chat_panel(timeout=15):
                    self.log("SKIP", f"Chat panel did not load for '{chat_name}'.")
                    self.clear_unread_status()
                    continue
                time.sleep(random.uniform(1.0, 1.8))

                # Group Header Metadata Filter Validation Check
                try:
                    header_subtext_element = self.driver.find_elements(By.XPATH, '//header//div[contains(@class, "x1f6k8lb")]//span[@title]')
                    if header_subtext_element:
                        header_text = header_subtext_element[0].get_attribute("title") or header_subtext_element[0].text
                        if "," in header_text or "click here for group info" in header_text.lower():
                            self.log("SKIP", f"Group layout identified via header metadata for '{chat_name}'. Skipping.")
                            self.clear_unread_status()
                            continue
                except:
                    pass

                input_test = self.driver.find_elements(By.XPATH, '//div[contains(@class, "lexical-rich-text-input")]//div[@contenteditable="true"]')
                if not input_test:
                    self.log("SKIP", f"Channel for '{chat_name}' is read-only. Bypassing.")
                    self.clear_unread_status()
                    continue

                incoming_bubbles = self.get_incoming_bubbles()
                if not incoming_bubbles:
                    self.log("SKIP", f"No incoming message bubble for '{chat_name}'.")
                    self.clear_unread_status()
                    continue

                last_bubble = incoming_bubbles[-1]
                rag_extra = ""
                user_query = ""
                memory_client_text = ""

                # Check 1: PDF / document attachment (download + extract text)
                download_buttons = last_bubble.find_elements(
                    By.XPATH,
                    './/button[@aria-label="Download"] | .//span[@data-icon="download"]/ancestor::div[@role="button"]',
                )
                pdf_indicators = last_bubble.find_elements(
                    By.XPATH,
                    './/span[contains(@title, ".pdf") or contains(@title, ".PDF")] | '
                    './/div[contains(text(), ".pdf") or contains(text(), ".PDF")] | '
                    './/span[contains(text(), ".pdf") or contains(text(), ".PDF")]',
                )
                doc_indicators = last_bubble.find_elements(By.XPATH, './/span[@data-icon="document"]')
                is_pdf_message = download_buttons and (pdf_indicators or doc_indicators)

                user_caption = self.extract_incoming_message_text(last_bubble)

                if is_pdf_message:
                    self.log("DATA", "PDF attachment detected — downloading and extracting text...")
                    download_buttons[0].click()
                    extraction = self.pdf_extractor.extract_latest_download(label=chat_name)
                    memory_client_text = user_caption
                    if extraction and extraction.get("ok"):
                        local_history.ingest_message(
                            self.tenant_id,
                            chat_name,
                            f"[System: Extracted PDF {extraction['filename']}]\n{extraction['full_text']}",
                            role="client"
                        )
                        user_query = user_caption or (
                            "The client sent a PDF document. Summarize the main points "
                            "and ask how you can help further."
                        )
                    else:
                        user_query = user_caption or "The client sent a PDF."
                        rag_extra = self.pdf_extractor.build_agent_message(extraction, user_caption)
                else:
                    user_query = self.extract_incoming_message_text(last_bubble)
                    memory_client_text = user_query

                if not user_query and not rag_extra:
                    self.log("SKIP", f"Could not read message text in '{chat_name}'.")
                    self.clear_unread_status()
                    continue

                dedupe_key = user_query or rag_extra
                log_snapshot = dedupe_key.replace("\n", " ")[:40]
                self.log("DATA", f"Message payload processed: \"{log_snapshot}...\"")

                flow_state = self.flow.states.get(chat_name, {}).get("step", "menu")
                dedupe_key = f"{flow_state}:{dedupe_key}"

                if self.processed_messages.get(chat_name) == dedupe_key:
                    self.log("SKIP", f"Already replied to this message in '{chat_name}'.")
                    self.clear_unread_status()
                    continue

                self.processed_messages[chat_name] = dedupe_key

                reading_delay = min(len(user_query or dedupe_key) * 0.012, 4.0)
                time.sleep(random.uniform(1.0 + reading_delay, 2.5 + reading_delay))

                self.reload_agent_config()
                ai_reply = self.reply_from_flow(chat_name, user_query, rag_extra)
                if not ai_reply:
                    self.log("RAG_ERROR", f"No RAG reply generated for '{chat_name}'.")
                elif self.send_reply(ai_reply):
                    self.log("SUCCESS", f"Automated follow-up sequence completed for '{chat_name}'.")
                    try:
                        self.remember_exchange(chat_name, memory_client_text, ai_reply)
                    except Exception as mem_err:
                        self.log("RAG_ERROR", f"Could not index exchange: {mem_err}")
                else:
                    self.log("ACTION_ERROR", f"Reply generated but failed to send for '{chat_name}'.")

                self.clear_unread_status()

            except Exception as e:
                self.log("CHAT_ERROR", f"Failed processing '{locals().get('chat_name', '?')}': {e}")
                self.clear_unread_status()
                continue

    def start_monitoring_loop(self):
        """Primary long-running execution thread container loop."""
        self.log("SYSTEM", "Agent is running.")
        self._emit("status", {"status": "running"})
        while self.running:
            try:
                time.sleep(random.uniform(2.2, 3.4))

                try:
                    _ = self.driver.current_window_handle
                except WebDriverException:
                    self.log("CRITICAL", "Browser closed. Stopping agent.")
                    break

                self.process_chats()

            except Exception as core_exception:
                self.log("SYSTEM_ERROR", f"Loop error: {core_exception}")
                time.sleep(5)

        self.shutdown()

    def shutdown(self):
        """Smoothly safely disconnect and close active instances."""
        self.log("SYSTEM", "De-allocating operational dependencies...")
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
        self.log("SYSTEM", "Offline.")

if __name__ == "__main__":
    bot = WhatsAppBusinessBot()
    bot.initialize_webdriver()
    bot.start_monitoring_loop()