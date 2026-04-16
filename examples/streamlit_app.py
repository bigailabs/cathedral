"""
Streamlit demo app for Cathedral deployment.

This file is deployed by 14_streamlit.py
"""
import streamlit as st
import random

st.set_page_config(page_title="Cathedral Streamlit Demo", page_icon="rocket")

st.title("Cathedral Streamlit Demo")
st.markdown("An interactive app deployed on Cathedral GPU cloud.")

# Sidebar
st.sidebar.header("Settings")
name = st.sidebar.text_input("Your name", "World")

# Main content
st.header(f"Hello, {name}!")

# Interactive counter with session state
if "count" not in st.session_state:
    st.session_state.count = 0

col1, col2, col3 = st.columns(3)
with col1:
    if st.button("Increment"):
        st.session_state.count += 1
with col2:
    if st.button("Decrement"):
        st.session_state.count -= 1
with col3:
    if st.button("Reset"):
        st.session_state.count = 0

st.metric("Counter", st.session_state.count)

# Chart demo
st.header("Random Data Chart")
chart_size = st.slider("Data points", 10, 100, 50)

if st.button("Generate New Data"):
    st.session_state.chart_data = [random.gauss(0, 1) for _ in range(chart_size)]

if "chart_data" not in st.session_state:
    st.session_state.chart_data = [random.gauss(0, 1) for _ in range(chart_size)]

st.line_chart(st.session_state.chart_data)

# Footer
st.divider()
st.caption("Deployed with Cathedral - https://basilica.ai")  # TODO(cathedral-rename): URL
