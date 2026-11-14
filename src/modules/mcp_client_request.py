import json


async def call_mcp_tool(client, name: str, arguments: dict):
    """MCPツールを指定した引数で呼び出す。

    結果がツールエラーなら説明付きのRuntimeErrorにし、成功時は応答をそのまま返す。
    """
    result = await client.call_tool(name, arguments)
    if result.is_error:
        message = "MCP tool call failed"
        if result.content and hasattr(result.content[0], "text"):
            message = result.content[0].text
        raise RuntimeError(message)
    return result

async def print_mcp_tool_list(client) -> list[dict]:
    """接続先で利用できるMCPツールの名前・説明・引数の定義を番号付きで表示する。"""
    result = await client.list_tools()
    tools_list = [
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": tool.input_schema,
        }
        for tool in result.tools
    ]
    for i, tool_data in enumerate(tools_list):
        print(f"{i+1}. {tool_data}")

async def print_mcp_resource_list(client) -> None:
    """接続先で利用できるMCPリソースの一覧を番号付きで表示する。"""
    result = await client.list_resources()
    for i, resource in enumerate(result.resources):
        print(f"{i+1}. {resource}")

def extract_json_object_from_tool_result(result, tool_name: str) -> dict:
    """MCPツールの構造化応答を優先し、なければテキスト内のJSONを読み、辞書を取り出す。

    取得できなければエラーにする。
    """
    structured_content = getattr(result, "structured_content", None)
    if structured_content is not None:
        wrapped_result = structured_content.get("result")
        if set(structured_content) == {"result"} and isinstance(wrapped_result, dict):
            return wrapped_result
        if isinstance(structured_content, dict):
            return structured_content

    for content in result.content:
        content_text = getattr(content, "text", None)
        if not isinstance(content_text, str):
            continue
        try:
            decoded_content = json.loads(content_text)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded_content, dict):
            return decoded_content
    raise RuntimeError(f"MCP tool '{tool_name}' returned no JSON object")


def extract_texts_from_tool_result(result, tool_name: str) -> list[str]:
    """MCPツール応答の各要素からテキストを取り出して一覧にする。

    テキスト以外の要素があればエラーにする。
    """
    text_results = []
    for content in result.content:
        content_text = getattr(content, "text", None)
        if not isinstance(content_text, str):
            raise RuntimeError(f"MCP tool '{tool_name}' returned non-text content")
        text_results.append(content_text)
    return text_results


def extract_value_from_tool_result(result):
    """MCPツール応答から、構造化データ、JSONとして読めるテキスト、通常文字列などを優先順位に従って取り出す。"""
    structured_content = getattr(result, "structured_content", None)
    if structured_content is not None:
        return structured_content
    if not result.content:
        return result

    for content in result.content:
        content_text = getattr(content, "text", None)
        if not isinstance(content_text, str):
            continue
        try:
            return json.loads(content_text)
        except json.JSONDecodeError:
            return content_text
    return result.content


def extract_json_object_from_resource_result(result, uri: str) -> dict:
    """MCPリソースの読み取り結果からJSONの辞書を取り出す。

    該当する内容がなければエラーにする。
    """
    for content in result.contents:
        content_text = getattr(content, "text", None)
        if not isinstance(content_text, str):
            continue

        try:
            decoded_content = json.loads(content_text)
        except json.JSONDecodeError:
            continue

        if isinstance(decoded_content, dict):
            return decoded_content

    raise RuntimeError(f"MCP resource '{uri}' returned no JSON object")
