"""One terminal input policy for clarification, approval and acceptance."""
from .task_memory import event


def read_reply(task, ask, prompt):
    """Ignore empty lines; only an explicit pause, EOF or batch mode pauses work."""
    if ask is None:
        event(task, "交互暂停", "非交互模式，等待用户回复。")
        return ""
    while True:
        try:
            reply = ask(prompt + "（输入 /pause 暂停）：").strip()
        except EOFError:
            event(task, "交互暂停", "终端输入已关闭，任务已保留，等待重新连接后回复。")
            return ""
        if reply.lower() == "/pause":
            event(task, "交互暂停", "用户明确输入 /pause。")
            return ""
        if reply:
            return reply
        print("未输入回答，继续等待；需要暂停请输入 /pause。", flush=True)
