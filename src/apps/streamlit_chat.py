import asyncio
import streamlit as st
from src.utils.chat_engine import ChatBot

st.set_page_config(
    page_icon="🤖",
    page_title="Dynatrace Multi-Agent Assistant",
    layout="wide",
    initial_sidebar_state="auto",
)

def initialize_session_state():
    if "chat_started" not in st.session_state:
        st.session_state.chat_started = False
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "chatbot" not in st.session_state:
        st.session_state.chatbot = ChatBot()

initialize_session_state()

with st.sidebar:
    st.subheader("⚙️ Dynatrace Assistant", divider="gray")
    st.write("Chatbot powered by `LangGraph` & `Groq` inference.")
    
    selected_model = st.selectbox(
        "LLM Model", 
        options=list(ChatBot.AVAILABLE_MODELS.keys()),
        key="model_select"
    )
    
    if selected_model != st.session_state.chatbot.model_name:
        st.session_state.chatbot.update_model(selected_model)
    
    thread_id = st.text_input("Dynatrace Session ID", value="001", key="thread_id")
    
    if not st.session_state.chat_started:
        if st.button("Start Chat Session", use_container_width=True):
            st.session_state.chat_started = True
            st.session_state.messages = []
            st.rerun()
    else:
        if st.button("End Chat Session", use_container_width=True, type="primary"):
            st.session_state.chat_started = False
            st.rerun()
        
        st.divider()
        
        with st.expander("Session History (JSON)", expanded=False):
            st.json(st.session_state.messages)

st.subheader("💬 Dynatrace Multi-Agent Chat", divider="gray")

if st.session_state.chat_started:
    if not st.session_state.messages:
        hello_message = ":sparkles: Hello! I am your Dynatrace AI Assistant. How can I help you today?"
        st.session_state.messages = [{"role": "assistant", "content": hello_message}]
    
    # Display past conversation
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    # New user input
    if prompt := st.chat_input("Type your question..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # Assistant response (streaming)
        with st.chat_message("assistant"):
            response_container = st.empty()
            response_parts = []

            async def run_stream():
                async for token in st.session_state.chatbot.chat(prompt, thread_id):
                    response_parts.append(token)
                    response_container.markdown("".join(response_parts))
                return "".join(response_parts)

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            response_str = loop.run_until_complete(run_stream())
            loop.close()

            st.session_state.messages.append(
                {"role": "assistant", "content": response_str}
            )
