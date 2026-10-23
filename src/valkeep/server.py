"""검토한 tools 기능만 제공하는 MCP 서버."""

from mcp.server.lowlevel import Server

from valkeep import __version__
from valkeep.proxy import Proxy


def build_server(proxy: Proxy) -> Server:
    server = Server("valkeep", version=__version__)

    @server.list_tools()
    async def list_tools():
        return proxy.tools()

    # SDK 기본 입력 오류는 인자를 포함할 수 있으므로 자체 검증 결과만 반환한다.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict):
        return await proxy.execute(name, arguments)

    return server
