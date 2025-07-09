import gradio as gr
import requests
import re
import json
from datetime import datetime
from dotenv import load_dotenv
import os

load_dotenv()

notion_token = os.getenv("notion_token")
database_id = os.getenv("database_id")

def parse_chat_text_to_messages(text, system_message):
    messages = [{"role": "system", "content": system_message}]
    user_pattern = re.compile(r"\[用户\](.*?)\[/用户\]", re.S)
    bot_pattern = re.compile(r"\[助手\](.*?)\[/助手\]", re.S)
    users = user_pattern.findall(text)
    bots = bot_pattern.findall(text)
    for u, b in zip(users, bots):
        messages.append({"role": "user", "content": u.strip()})
        messages.append({"role": "assistant", "content": b.strip()})
    return messages

def format_messages_to_chat_text(messages):
    texts = []
    for i in range(1, len(messages), 2):
        if i + 1 >= len(messages):
            break
        user = messages[i]["content"]
        assistant = messages[i+1]["content"]
        block = f"[用户]\n{user}\n[/用户]\n[助手]\n{assistant}\n[/助手]"
        texts.append(block)
    return "\n\n---\n\n".join(texts)

def messages_to_chatbot(messages):
    # 直接过滤掉系统消息，返回剩余消息列表
    return [m for m in messages if m["role"] != "system"]

def format_think_text(text):
    def repl(m):
        content = m.group(1).strip()
        return f"<details><summary>思考</summary>\n\n{content}\n\n</details>"
    return re.sub(r"<think>(.*?)</think>", repl, text, flags=re.S)

def send_message_non_stream(chat_text, user_input, chatbot_data, system_message, model, api_url, api_key, temperature, max_tokens):
    messages = parse_chat_text_to_messages(chat_text, system_message)
    messages.append({"role": "user", "content": user_input.strip()})
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, "stream": False}
    response = requests.post(api_url, headers=headers, json=payload)
    response.raise_for_status()
    data = response.json()
    bot_reply_raw = data['choices'][0]['message']['content'].strip()
    # 非流式调用 format_think_text 转换
    bot_reply = format_think_text(bot_reply_raw)
    messages.append({"role": "assistant", "content": bot_reply})
    new_chat_text = format_messages_to_chat_text(messages)
    new_chatbot = messages_to_chatbot(messages)
    return new_chat_text, new_chatbot, ""

def send_message(chat_text, user_input, chatbot_data, system_message, model, api_url, api_key, temperature, max_tokens, stream_mode):
    messages = parse_chat_text_to_messages(chat_text, system_message)
    messages.append({"role": "user", "content": user_input.strip()})

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream_mode
    }

    if stream_mode:
        response = requests.post(api_url, headers=headers, json=payload, stream=True)
        response.raise_for_status()

        partial_reply = ""
        for line in response.iter_lines():
            if line:
                text_line = line.decode('utf-8').strip()
                if text_line.startswith("data: "):
                    text_line = text_line[len("data: "):]
                if text_line == "[DONE]":
                    break
                try:
                    data = json.loads(text_line)
                    delta = data['choices'][0].get('delta', {})
                    content_piece = delta.get('content', '')
                    partial_reply += content_piece
                    new_messages = messages + [{"role": "assistant", "content": partial_reply}]
                    # **流式输出时不转换 <think> 标签，保持原样**
                    new_chat_text = format_messages_to_chat_text(new_messages)
                    new_chatbot = messages_to_chatbot(new_messages)
                    yield new_chat_text, new_chatbot, ""
                except Exception:
                    continue
        return
    else:
        # 非流式调用 format_think_text 转换
        new_chat_text, new_chatbot, _ = send_message_non_stream(chat_text, user_input, chatbot_data, system_message, model, api_url, api_key, temperature, max_tokens)
        yield new_chat_text, new_chatbot, ""

def update_system_message_from_dropdown(choice):
    presets = {"默认提示": "You are a helpful assistant.", "/no_think": "/no_think"}
    return presets.get(choice, "")

def write_to_notion(summary, notion_token, database_id):
    now = datetime.now()
    timestamp = now.isoformat()
    date_str = now.strftime("%Y-%m-%d %H:%M:%S")

    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    payload = {
        "parent": {"database_id": database_id},
        "properties": {
            "时间": {"title": [{"text": {"content": date_str}}]},
            "总结文本": {"rich_text": [{"text": {"content": summary}}]},
            "时间戳": {"date": {"start": timestamp}}
        }
    }
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        return "✅ 总结内容已写入 Notion"
    else:
        return f"❌ 写入 Notion 失败: {response.text}"

def generate_summary(chat_text, summary_prompt, system_message, model, api_url, api_key, temperature, max_tokens, notion_token, database_id):
    content_to_summarize = summary_prompt.strip() + "\n\n" + chat_text.strip()
    messages = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": content_to_summarize}
    ]
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, "stream": False}
    response = requests.post(api_url, headers=headers, json=payload)
    response.raise_for_status()
    data = response.json()
    summary = data['choices'][0]['message']['content'].strip()
    return summary

def get_notion_database_schema(notion_token, database_id):
    url = f"https://api.notion.com/v1/databases/{database_id}"
    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Notion-Version": "2022-06-28"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    props = data.get("properties", {})
    lines = [f"📊 数据库名称: {data.get('title', [{'text': {'content': '未知'}}])[0]['text']['content']}"]
    lines.append("字段列表：")
    for key, val in props.items():
        field_type = val.get("type", "unknown")
        lines.append(f"• {key} ({field_type})")
    return "\n".join(lines)

def query_notion_database(notion_token, database_id):
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    results = []
    has_more = True
    next_cursor = None

    while has_more:
        payload = {"page_size": 100}
        if next_cursor:
            payload["start_cursor"] = next_cursor

        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

        for page in data.get("results", []):
            title = "无标题"
            properties = page.get("properties", {})
            for key, val in properties.items():
                if val.get("type") == "title":
                    title_items = val.get("title", [])
                    if title_items:
                        title = title_items[0].get("text", {}).get("content", "无标题")
                    break
            results.append(f"- {title} (ID: {page['id']})")

        has_more = data.get("has_more", False)
        next_cursor = data.get("next_cursor", None)

    return "📄 数据库页面内容：\n" + "\n".join(results)

# 🧱 Gradio App UI
with gr.Blocks() as demo:
    gr.Markdown("# 🤖 自定义系统消息 + 一键总结 + 写入 Notion")

    with gr.Row():
        with gr.Column(scale=1):
            system_message_dropdown = gr.Dropdown(label="选择系统消息预设", choices=["/no_think", "默认提示"], value="/no_think")
            system_message = gr.Textbox(label="系统消息（支持编辑）", value="/no_think", lines=2)
            chat_text = gr.TextArea(label="对话内容（可编辑）", lines=25)
        with gr.Column(scale=1):
            chatbot = gr.Chatbot(label="聊天窗口", height=800, elem_id="chatbot", type="messages")

    system_message_dropdown.change(update_system_message_from_dropdown, inputs=system_message_dropdown, outputs=system_message)

    with gr.Row():
        user_input = gr.Textbox(placeholder="请输入你的问题...", show_label=False)
        send_btn = gr.Button("发送", variant="primary")

    with gr.Row():
        api_url = gr.Textbox(label="API URL", value="http://localhost:8001/v1/chat/completions")
        api_key = gr.Textbox(label="API Key", value="vllm", type="password")
        model = gr.Textbox(label="模型名称", value="Qwen3-32B")
        temperature = gr.Slider(label="温度", minimum=0, maximum=1, step=0.01, value=0.7)
        max_tokens = gr.Number(label="最大Token数", value=12800, precision=0)
        stream_mode = gr.Checkbox(label="开启流式输出", value=True)

    send_btn.click(
        send_message,
        inputs=[chat_text, user_input, chatbot, system_message, model, api_url, api_key, temperature, max_tokens, stream_mode],
        outputs=[chat_text, chatbot, user_input]
    )
    user_input.submit(
        send_message,
        inputs=[chat_text, user_input, chatbot, system_message, model, api_url, api_key, temperature, max_tokens, stream_mode],
        outputs=[chat_text, chatbot, user_input]
    )

    with gr.Accordion("📝 总结与 Notion 功能", open=True):
        with gr.Row():
            summary_prompt = gr.Textbox(label="总结提示词", value="请总结以下对话内容，突出重点和关键结论：", lines=2)
            notion_token = gr.Textbox(label="Notion Token", type="password",value=notion_token)
            database_id = gr.Textbox(label="Notion 数据库ID", type="password",value=database_id)

        with gr.Row():
            summary_btn = gr.Button("一键总结", variant="secondary")
            write_summary_btn = gr.Button("写入总结到 Notion", variant="primary")
            get_schema_btn = gr.Button("获取数据库结构")
            query_btn = gr.Button("获取数据库所有页面内容")

        summary_output = gr.Textbox(label="总结输出结果", lines=6, interactive=True)
        write_result_md = gr.Textbox(label="写入 Notion 结果", lines=1, interactive=False)
        schema_output = gr.Textbox(label="数据库结构", lines=8, interactive=False)
        query_output = gr.Textbox(label="数据库页面列表", lines=10, interactive=False)

        write_summary_btn.click(
            write_to_notion,
            inputs=[summary_output, notion_token, database_id],
            outputs=write_result_md
        )
        summary_btn.click(
            generate_summary,
            inputs=[chat_text, summary_prompt, system_message, model, api_url, api_key, temperature, max_tokens, notion_token, database_id],
            outputs=summary_output
        )

        get_schema_btn.click(
            get_notion_database_schema,
            inputs=[notion_token, database_id],
            outputs=schema_output
        )

        query_btn.click(
            query_notion_database,
            inputs=[notion_token, database_id],
            outputs=query_output
        )

demo.launch(share=False,mcp_server=True)
