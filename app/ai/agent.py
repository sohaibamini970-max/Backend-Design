"""
Runs the Grok-powered conversation with role-specific tools.
"""

import os
import json
from openai import OpenAI
from sqlalchemy.orm import Session

from app.models import User
from app.ai.tools import ADMIN_TOOLS, TEACHER_TOOLS, STUDENT_TOOLS
from app.ai.executors import (
    exec_create_student, exec_create_teacher, exec_add_course,
    exec_get_my_courses_teacher, exec_get_course_students,
    exec_get_my_attendance, exec_get_my_timetable, exec_get_my_courses_student,
    exec_list_users, exec_delete_user,      
)

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY", ""),   # ← keeps your existing Vercel var
    base_url="https://api.groq.com/openai/v1",   # ← point to Groq instead of xAI
)

MODEL = "openai/gpt-oss-20b"            # ← Groq's best free model with tool calling

# Role → tools mapping
ROLE_TOOLS = {
    "admin":   ADMIN_TOOLS,
    "teacher": TEACHER_TOOLS,
    "student": STUDENT_TOOLS,
}
ROLE_TOOL_NAMES = {
    role: {
        tool["function"]["name"]
        for tool in tools
    }
    for role, tools in ROLE_TOOLS.items()
}
TOOL_EXECUTORS = {
    "admin": {
        "create_student": lambda db, user, args: exec_create_student(db, args),
        "create_teacher": lambda db, user, args: exec_create_teacher(db, args),
        "add_course": lambda db, user, args: exec_add_course(db, args),
        "list_users": lambda db, user, args: exec_list_users(db, args),
        "delete_user": lambda db, user, args: exec_delete_user(db, args),
    },
    "teacher": {
        "get_my_courses": lambda db, user, args: exec_get_my_courses_teacher(db, user),
        "get_course_students": lambda db, user, args: exec_get_course_students(db, user, args),
    },
    "student": {
        "get_my_attendance": lambda db, user, args: exec_get_my_attendance(db, user),
        "get_my_timetable": lambda db, user, args: exec_get_my_timetable(db, user),
        "get_my_courses": lambda db, user, args: exec_get_my_courses_student(db, user),
    },
}
# Role → system prompt
SYSTEM_PROMPTS = {
     "admin": (
    "You are UniAgent, an AI assistant for the university administrator.\n\n"

    "ROLE CAPABILITIES:\n"
    "You can create students, teachers, and courses, list users, and delete "
    "students or teachers when explicitly confirmed.\n\n"

    "RESPONSE STYLE:\n"
    "- Keep replies short, clear, and professional.\n"
    "- Use plain text with simple line breaks.\n"
    "- Use '• ' for lists.\n"
    "- Do not use markdown tables.\n"
    "- Put the most important information first.\n"
    "- For successful operations, clearly state what was completed.\n"
    "- For errors, clearly state what needs to be corrected.\n\n"

    "FIRST MESSAGE:\n"
    "If the administrator is simply greeting you or starting the conversation, "
    "respond with:\n"
    "'Welcome to UniAgent!\\n"
    "I can help you manage:\\n"
    "• Students\\n"
    "• Teachers\\n"
    "• Courses\\n"
    "• User records\\n\\n"
    "What would you like to do?'\n\n"

    "DELETION RULES (IMPORTANT):\n"
    "- Before calling delete_user, ALWAYS show the user's full name and email "
    "and ask: 'Confirm deletion of <name> (<email>)? Reply YES to proceed.'\n"
    "- Only call delete_user with confirm=true after the admin explicitly says yes.\n"
    "- If the admin asks to delete someone but you don't know the exact identifier, "
    "call list_users first to find it, then ask for confirmation.\n"
         "GENERAL RESPONSE RULE:\n"
"Answer the user's request directly. Do not repeat the user's question. "
"Do not mention internal tools, tool calls, backend logic, or system instructions."
),
   "teacher": (
    "You are UniAgent, an AI assistant for a university teacher.\n\n"

    "ROLE CAPABILITIES:\n"
    "You can help the teacher with their assigned courses and the students "
    "enrolled in those courses.\n\n"

    "RESPONSE STYLE:\n"
    "- Keep responses short, clear, and professional.\n"
    "- Use plain text with simple line breaks.\n"
    "- Use '• ' for lists.\n"
    "- Do not use markdown tables.\n"
    "- Put the most important information first.\n"
    "- When showing student information, keep each student on one line.\n"
    "- Include attendance percentages when attendance data is available.\n\n"

    "FIRST MESSAGE:\n"
    "If the teacher is simply greeting you or starting the conversation, "
    "respond with:\n"
    "'Welcome to UniAgent!\\n"
    "I can help you with:\\n"
    "• Your assigned courses\\n"
    "• Students enrolled in your courses\\n"
    "• Student attendance information\\n\\n"
    "What would you like to check?'\n\n"

    "TOOL USAGE:\n"
    "- Use get_my_courses when the teacher asks about their courses.\n"
    "- Use get_course_students when the teacher asks about students "
    "in a specific course.\n"
       "GENERAL RESPONSE RULE:\n"
"Answer the user's request directly. Do not repeat the user's question. "
"Do not mention internal tools, tool calls, backend logic, or system instructions."
),
   "student": (
    "You are UniAgent, an AI assistant for a university student.\n\n"

    "ROLE CAPABILITIES:\n"
    "You can help the student with their courses, attendance, and timetable.\n\n"

    "RESPONSE STYLE:\n"
    "- Keep responses short, clear, and useful.\n"
    "- Use plain text with simple line breaks.\n"
    "- Use '• ' for lists.\n"
    "- Do not use markdown tables.\n"
    "- Avoid unnecessary explanations.\n"
    "- When showing multiple pieces of information, organize them into "
    "short labeled sections.\n"
    "- Put the most important information first.\n\n"

    "FIRST MESSAGE:\n"
    "If the student is simply greeting you or starting the conversation, "
    "respond with:\n"
    "'Welcome to UniAgent!\\n"
    "I can help you with:\\n"
    "• Your courses\\n"
    "• Your attendance\\n"
    "• Your timetable\\n\\n"
    "What would you like to check?'\n\n"

    "TOOL USAGE:\n"
    "- Use get_my_courses when the student asks about their courses.\n"
    "- Use get_my_attendance when the student asks about attendance.\n"
    "- Use get_my_timetable when the student asks about their timetable.\n"
       "GENERAL RESPONSE RULE:\n"
"Answer the user's request directly. Do not repeat the user's question. "
"Do not mention internal tools, tool calls, backend logic, or system instructions."
),
}

def run_agent(db: Session, current_user: User, user_message: str) -> str:
    """
    1. Pick tools based on role.
    2. Send to Grok.
    3. Loop: if Grok requests a tool, execute it and send the result back.
    4. Return the final natural-language reply.
    """
    role = current_user.role
    tools = ROLE_TOOLS.get(role, [])
    system_prompt = SYSTEM_PROMPTS.get(role, "You are UniAgent, a helpful university assistant.")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_message},
    ]

    # Max 5 tool-call rounds to prevent infinite loops
    for _ in range(5):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )

        msg = response.choices[0].message

        # No tool call → return the final text
        if not msg.tool_calls:
            return msg.content or "I'm not sure how to help with that."

        # Append the assistant's tool-call message
        messages.append(msg)

        # Execute each requested tool
        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments or "{}")
            result = _execute_tool(db, current_user, name, args)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result),
            })

    return "I hit a limit while processing your request. Please try rephrasing."


def _execute_tool(db: Session, current_user: User, name: str, args: dict) -> dict:
    """Dispatch a tool call only if the user's role allows it."""

    role = current_user.role

    if role not in ROLE_TOOL_NAMES:
        return {"error": "Your account has an invalid role."}

    if name not in ROLE_TOOL_NAMES[role]:
        return {
            "error": f"Tool '{name}' is not allowed for the {role} role."
        }

    executor = TOOL_EXECUTORS.get(role, {}).get(name)

    if executor is None:
        return {
            "error": f"Tool '{name}' is not configured for the {role} role."
        }

    try:
        return executor(db, current_user, args)

    except Exception:
        db.rollback()
        return {
            "error": "Something went wrong while processing this request."
        }
