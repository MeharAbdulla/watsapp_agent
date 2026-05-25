import os
import google.generativeai as genai

def generate_gemini_reply(chat_name: str, user_message: str, agent_config: dict, recent_history: list = None) -> str:
    """Generate a response using Gemini Chat API."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return "System error: GEMINI_API_KEY is not configured."
    genai.configure(api_key=api_key)
    
    model_name = agent_config.get("gemini_chat_model", "gemini-2.5-flash")
    business_name = agent_config.get("business_name", "Our Business")
    extra_knowledge = agent_config.get("extra_knowledge", "")
    
    system_instruction = f"You are a helpful AI assistant for {business_name} on WhatsApp.\n"
    if extra_knowledge:
        system_instruction += f"\nBusiness Knowledge:\n{extra_knowledge}\n"
    
    system_instruction += "\nKeep responses relatively brief and conversational, suitable for WhatsApp."
    
    model = genai.GenerativeModel(model_name=model_name, system_instruction=system_instruction)
    
    contents = []
    if recent_history:
        for msg in recent_history[-10:]: # Pass only last 10 messages for context
            role = "user" if msg["role"] == "client" else "model"
            contents.append({"role": role, "parts": [msg["text"]]})
    
    contents.append({"role": "user", "parts": [user_message]})
    
    try:
        response = model.generate_content(contents)
        return response.text
    except Exception as e:
        return f"Sorry, I am currently unable to process your request. ({str(e)})"
