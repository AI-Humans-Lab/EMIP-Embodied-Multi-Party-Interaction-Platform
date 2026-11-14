import asyncio
import base64
import uuid
from contextlib import asynccontextmanager
from websockets.legacy.client import connect
import json
import os
import requests
from time import perf_counter
from datetime import datetime
from pathlib import Path
from typing import Callable
from openai import OpenAI

try:
    from .utils import make_file_logger
# スクリプト単体実行時用（フォールバック）
except ImportError:
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from modules.utils import make_file_logger

TIMESTR_FORMAT = '%Y%m%d_%H%M%S_%f'
DECISION_MODEL = "gpt-6-luna"


def request_decision(call_id, text, question, *, log_decision):
    """Decisions APIに行動の必要性を問い合わせ、返されたJSONを解析して返す。

    要求・応答・失敗と所要時間を記録する。通信中に他の処理を止めないよう、呼び出し元で別スレッドに移して実行する。
    """
    url = "https://api.openai.com/v1/decisions"
    body = {"model": DECISION_MODEL, "input": text, "questions": [
        {"type": "predicate", "name": "needs_action", "instructions": question}
    ]}
    log_decision(call_id, "request", method="POST", url=url, timeout_seconds=60, body=body)
    started_at = perf_counter()
    response = None
    try:
        response = requests.post(url, headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, json=body, timeout=60)
        elapsed = perf_counter() - started_at
        log_decision(
            call_id, "response", elapsed_seconds=elapsed,
            status_code=response.status_code,
            headers={k: v for k, v in response.headers.items()
                     if k.lower() in {"x-request-id", "openai-processing-ms", "retry-after", "content-type"}
                     or k.lower().startswith("x-ratelimit-")},
            body_text=response.text,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError, KeyError) as error:
        log_decision(call_id, "request_error",
                     elapsed_seconds=perf_counter() - started_at,
                     error_type=type(error).__name__, message=str(error))
        raise
    finally:
        if response is not None:
            response.close()
    return result


def read_action_probability(result):
    """APIの応答から「行動が必要である確率」を取り出して返す。

    needs_actionという判定が一つだけ存在することと、確率が0から1の数値であることを確認する。不正な応答はValueErrorにする。
    """
    if not isinstance(result, dict) or not isinstance(result.get("answers"), list):
        raise ValueError("Invalid Decisions API response")
    answers = [
        answer for answer in result["answers"]
        if isinstance(answer, dict) and answer.get("name") == "needs_action"
    ]
    if len(answers) != 1 or answers[0].get("type") != "predicate":
        raise ValueError("Decisions API refused or returned no unique predicate")
    probability = answers[0].get("probability")
    # Although bool is a subtype of int, do not accept it as an API probability.
    if type(probability) not in (int, float) or not 0 <= probability <= 1:
        raise ValueError("Invalid Decisions API probability")
    return probability


def _format_openai_tools(tools):
    """利用可能なツールの名前・説明・引数定義を、OpenAIの関数呼び出し形式へ変換する。"""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        }
        for tool in tools
    ]


def _format_assistant_tool_calls(message):
    """LLMが要求したツール呼び出しを、次のLLMリクエストの会話履歴に追加できる形式へ整える。"""
    return {
        "role": "assistant",
        "content": message.content,
        "tool_calls": [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                },
            }
            for tool_call in message.tool_calls
        ],
    }


async def _request_completion(client, model_name, temperature, request_timeout, max_trial, logger, messages, openai_tools=None, response_format=None):
    """設定されたモデルと会話履歴でLLMへ応答を要求する。

    失敗時は規定回数まで再試行し、成功した応答の最初のメッセージを返す。
    """
    request_args = {
        "model": model_name,
        "messages": list(messages),
        "temperature": temperature,
        "timeout": request_timeout,
    }
    if openai_tools is not None:
        request_args["tools"] = openai_tools
        request_args["tool_choice"] = "auto"
    if response_format is not None:
        request_args["response_format"] = response_format

    last_error = None
    for _ in range(max_trial):
        logger.info("start llm chat create")
        try:
            create_completion = client.chat.completions.create
            response = await asyncio.to_thread(create_completion, **request_args)
        except Exception as error:
            last_error = error
            logger.warning("Failed to request LLM completion: %s", error)
            continue
        logger.info("finish llm chat create")
        return response.choices[0].message

    raise RuntimeError(f"Failed to request LLM completion in {max_trial} trials") from last_error


async def _execute_tool_calls(tool_calls, tool_executor, available_tool_names, logger):
    """LLMが要求した各ツールの引数を検証して実行し、呼び出しID付きの結果メッセージを作る。

    ツール実行の例外も結果としてLLMへ返せる形にする。
    """
    tool_messages = []
    for tool_call in tool_calls:
        tool_name = tool_call.function.name
        try:
            tool_arguments = json.loads(tool_call.function.arguments)
            if not isinstance(tool_arguments, dict):
                raise TypeError("Tool arguments must be a JSON object")
            if tool_name not in available_tool_names:
                raise ValueError(f"Unknown tool: {tool_name}")
            tool_result = await tool_executor(tool_name, tool_arguments)
        except Exception as error:
            logger.warning("Tool '%s' failed: %s", tool_name, error)
            tool_result = {"error": str(error)}

        tool_messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps(tool_result, ensure_ascii=False),
        })
    return tool_messages


def _finalize_response(message, log_prompt, response_parser, log_dir, logger):
    """LLMの応答本文を必要に応じて解析し、プロンプトと応答をファイルへ記録する。

    生成結果と記録用の時刻文字列を返す。
    """
    content = message.content
    if content is None:
        raise ValueError("LLM returned neither content nor tool calls")

    try:
        generated = response_parser(content) if response_parser is not None else content
    except Exception as error:
        logger.warning("Failed to parse LLM response: %s", error)
        raise

    timestr = datetime.now().strftime(TIMESTR_FORMAT)
    prompt_path = Path(log_dir) / f"llm_{timestr}_prompt.txt"
    response_path = Path(log_dir) / f"llm_{timestr}_response.txt"
    with prompt_path.open("w", encoding="utf-8") as file:
        file.write(log_prompt)
    with response_path.open("w", encoding="utf-8") as file:
        file.write(content)

    print("\033[32m\n###### LLM ###### \033[0m")
    print(f"\033[32m{log_prompt}\033[0m")
    print("\033[31m----------------------------\033[0m")
    print(f"\033[31m{generated}\033[0m")
    return generated, timestr

def parse_javascript_action(message, disallowed_expressions=None):
    """LLMのJSON応答から1行ずつのJavaScript行動コードを取り出し、禁止文字列を検査してawaitと終端記号を補ったコードへ変換する。"""
    disallowed_expressions = disallowed_expressions or []
    parsed = json.loads(message)
    lines = parsed["code"]
    code = ""
    for line in lines:
        if not isinstance(line, str):
            raise TypeError("Each entry in code must be a string")
        if "\n" in line or "\r" in line:
            raise ValueError("Each entry in code must be a single line")
        code += f"await {line};\n"

    for expression in disallowed_expressions:
        if expression in code:
            raise ValueError(f"Disallowed expression: {expression}")
    return code

class OpenAILLM:
    def __init__(self, 
                 log_dir:str, 
                 api_key:str, 
                 model_name:str="gpt-6-luna", 
                 temperature:float=1.0, 
                 request_timeout:int=120,
                 max_trial:int=3,
                 ):
        """LLMの設定、APIクライアント、ログ出力先を用意する。"""
        self.api_key = api_key
        self.model_name= model_name
        self.temperature=temperature
        self.request_timeout = request_timeout
        self.max_trial = max_trial
        self.log_dir = log_dir
        self.client = OpenAI(api_key=self.api_key)
        self.logger = make_file_logger(f"llm", f"{log_dir}/llm.log")
        print(f"openai LLM __init__ finish")

    def format_prompts_for_llm(self, prompts: list[tuple[str, str]]) -> list[dict]:
        """入力プロンプトをsystem・user・assistantの会話メッセージへ整える。

        未対応の役割はエラーにする。
        """
        formatted = []
        for role, content in prompts:
            if role in ("user", "system", "assistant"):
                formatted.append({"role": role, "content": content})
            else:
                raise ValueError(f"対応したメッセージ形式がありません: {role}")
        return formatted

    async def generate(self, prompts, *, tools=None, tool_executor=None, response_parser=None, response_format=None, max_tool_rounds=8):
        """LLMへ問い合わせ、要求されたツールを実行した結果を会話へ追加して応答を求め直す。

        最終回答を解析・保存して返し、ツール実行の反復回数には上限を設ける。
        """
        client = self.client
        model_name = self.model_name
        temperature = self.temperature
        request_timeout = self.request_timeout
        max_trial = self.max_trial
        log_dir = self.log_dir
        logger = self.logger
        tools = [] if tools is None else list(tools)
        messages = [dict(prompt) for prompt in prompts]

        if not tools:
            message = await _request_completion(client, model_name, temperature, request_timeout, max_trial, logger, messages, response_format=response_format)
            log_prompt = "".join(prompt["content"] for prompt in prompts)
            return _finalize_response(message, log_prompt, response_parser, log_dir, logger)

        if tool_executor is None:
            raise ValueError("tool_executor is required when tools are provided")
        if max_tool_rounds < 0:
            raise ValueError("max_tool_rounds must not be negative")

        openai_tools = _format_openai_tools(tools)
        available_tool_names = {tool["name"] for tool in tools}
        message = await _request_completion(client, model_name, temperature, request_timeout, max_trial, logger, messages, openai_tools, response_format)

        for _ in range(max_tool_rounds):
            tool_calls = message.tool_calls or []
            if not tool_calls:
                log_prompt = json.dumps(messages, ensure_ascii=False, indent=2)
                return _finalize_response(message, log_prompt, response_parser, log_dir, logger)

            messages.append(_format_assistant_tool_calls(message))
            tool_messages = await _execute_tool_calls(tool_calls, tool_executor, available_tool_names, logger)
            messages.extend(tool_messages)
            message = await _request_completion(client, model_name, temperature, request_timeout, max_trial, logger, messages, openai_tools, response_format)

        if message.tool_calls:
            raise RuntimeError(f"LLM exceeded the limit of {max_tool_rounds} tool rounds")
        log_prompt = json.dumps(messages, ensure_ascii=False, indent=2)
        return _finalize_response(message, log_prompt, response_parser, log_dir, logger)

    async def request_llm(self, prompts:list, disallowed_expressions=None, javascript_check=True):
        """従来の呼び出し口から生成処理を実行する。

        JavaScript生成か通常テキストかに応じて、応答の解析方法を選ぶ。
        """
        disallowed_expressions = disallowed_expressions or []
        if javascript_check:
            parser = lambda content: parse_javascript_action(content, disallowed_expressions)
            response_format = {"type": "json_object"}
        else:
            parser = None
            response_format = None
        return await self.generate(prompts, response_parser=parser, response_format=response_format)

LIVE_URL = "wss://api.openai.com/v1/live/sessions"


@asynccontextmanager
async def open_gpt_live_session(api_key):
    """指定のAPIキーでGPT-Liveへ接続し、セッション操作の窓口を渡して、利用後に接続を閉じる。"""
    async with connect(
        LIVE_URL,
        extra_headers={"Authorization": "Bearer " + api_key},
        open_timeout=20,
        close_timeout=5,
        max_size=4 * 1024 * 1024,
    ) as websocket:
        yield GptLiveSession(websocket)


class GptLiveSession:
    """GPT-Liveの接続上で、既存のセッション・音声・テキストの通信形式を扱う。"""

    def __init__(self, websocket):
        """接続済みWebSocketを保持する。MCPや会話履歴は作成しない。"""
        self.websocket = websocket

    async def start(
        self,
        *,
        instructions: str = "日本語で自然に会話してください。",
        voice_name="marin",
        on_text_sent: Callable[[str], None] | None = None,
    ):
        """指示文・音声形式・委譲設定を送ってセッション開始を待ち、開始前のエラーや終了を通知する。

        指示文と送信記録用の同期コールバックを引数で受け取る。コールバックが指定された場合は、開始要求の送信後、開始応答の受信前に既存形式の記録を渡す。
        """
        await self.websocket.send(
            json.dumps(
                {
                    "type": "session.start",
                    "session": {
                        "model": "gpt-live-1",
                        "instructions": instructions,
                        "audio": {
                            "format": {"type": "audio/pcm", "rate": 24000},
                            "output": {"voice": voice_name},
                        },
                        "delegation": {"type": "client"},
                    },
                }
            )
        )
        if on_text_sent is not None:
            on_text_sent(json.dumps({
                "type": "session.start",
                "session": {"instructions": instructions},
            }, ensure_ascii=False))
        async with asyncio.timeout(20):
            while True:
                live_event = json.loads(await self.websocket.recv())
                if live_event["type"] == "error":
                    raise RuntimeError(f"GPT-Live startup error: {live_event.get('error')}")
                if live_event["type"] == "session.started":
                    break
                if live_event["type"] == "session.closed":
                    raise RuntimeError("GPT-Live closed before startup completed")

    async def send_input_audio(self, input_pcm24k_frame):
        """24kHzのPCMフレームをBase64へ変換し、入力音声イベントとして送信する。"""
        await self.websocket.send(
            json.dumps(
                {
                    "type": "session.input_audio.append",
                    "audio": base64.b64encode(input_pcm24k_frame).decode(
                        "ascii"
                    ),
                }
            )
        )

    async def receive_event(self):
        """GPT-Liveから1件の応答を受信し、JSONとして解析して返す。"""
        return json.loads(await self.websocket.recv())

    async def send(self, payload):
        """組み立て済みのテキストイベントを、内容を変更せずGPT-Liveへ送信する。"""
        await self.websocket.send(payload)

    async def close(self):
        """既存の形式でセッション終了を要求する。応答待ちと音声の排出は呼び出し側で行う。"""
        await self.websocket.send(json.dumps({"type": "session.close"}))


def split_text_by_utf8_byte_limit(text, byte_limit=400):
    """文章を文字の途中で切らず、UTF-8のバイト数を基準に分割して順に返す。

    GPT-Liveへのテキスト送信単位に使う。
    """
    text_chunk = ""
    for character in text:
        if len((text_chunk + character).encode("utf-8")) > byte_limit:
            yield text_chunk
            text_chunk = ""
        text_chunk += character
    if text_chunk:
        yield text_chunk

async def send_gpt_live_text(send_text, event_type, content, delegation_id=None):
    """文章をUTF-8のバイト数で分割し、イベント種別と必要な委譲IDを付けてGPT-Liveへ順番に送る。"""
    for text_chunk in split_text_by_utf8_byte_limit(content):
        live_event = dict(
            type=event_type,
            event_id=uuid.uuid4().hex,
            delegation_id=delegation_id,
            content=text_chunk,
        )
        await send_text(json.dumps(live_event, ensure_ascii=False))
        await asyncio.sleep(0)


if __name__ == "__main__":
    llm = OpenAILLM("logs", api_key=f"sk-xxx")

    prompt = llm.format_prompts_for_llm([("system", "こんにちは"), ("user", "こんにちは")])

    output = llm.request_llm(prompts=prompt, disallowed_expressions=[])

    print(f"{output}")
