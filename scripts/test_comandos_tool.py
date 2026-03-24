"""Test suite for the ejecutar_comando tool and multimedia response flow."""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_tool_factory():
    """Test that _create_comandos_tool produces a valid LangChain tool."""
    from app.agents.conversational import _create_comandos_tool, EJECUTAR_COMANDO_TOOL_NAME

    tool = _create_comandos_tool(999)

    assert tool.name == "ejecutar_comando", f"Expected 'ejecutar_comando', got '{tool.name}'"
    assert EJECUTAR_COMANDO_TOOL_NAME == "ejecutar_comando"

    # Check the tool has proper args_schema
    schema = tool.args_schema
    assert schema is not None, "Tool should have args_schema"
    fields = schema.model_fields
    assert "comando" in fields, "Missing 'comando' param"
    assert "solicitud" in fields, "Missing 'solicitud' param"
    assert "extra" in fields, "Missing 'extra' param"

    print("  [PASS] Tool factory creates valid tool with correct name and params")
    return tool


def test_tool_valid_commands(tool):
    """Test tool execution with valid commands."""

    async def _run():
        # Test image command
        result = await tool.ainvoke({
            "comando": "image",
            "solicitud": "https://example.com/photo.jpg",
            "extra": "Mira esta imagen",
        })
        parsed = json.loads(result)
        assert parsed["__comando__"] is True
        assert parsed["comando"] == "image"
        assert parsed["solicitud"] == "https://example.com/photo.jpg"
        assert parsed["extra"] == "Mira esta imagen"
        assert parsed["contacto_id"] == 999
        print("  [PASS] image command returns correct JSON")

        # Test audio command
        result = await tool.ainvoke({
            "comando": "audio",
            "solicitud": "https://example.com/audio.mp3",
            "extra": "",
        })
        parsed = json.loads(result)
        assert parsed["comando"] == "audio"
        assert parsed["extra"] == ""
        print("  [PASS] audio command returns correct JSON")

        # Test video command
        result = await tool.ainvoke({
            "comando": "VIDEO",  # test case-insensitivity
            "solicitud": "https://example.com/video.mp4",
            "extra": "Video tutorial",
        })
        parsed = json.loads(result)
        assert parsed["comando"] == "video"
        print("  [PASS] video command (case-insensitive) returns correct JSON")

        # Test monica command
        result = await tool.ainvoke({
            "comando": "monica",
            "solicitud": "Analiza el perfil del cliente",
            "extra": "Es un prospecto de ventas",
        })
        parsed = json.loads(result)
        assert parsed["comando"] == "monica"
        assert parsed["solicitud"] == "Analiza el perfil del cliente"
        print("  [PASS] monica command returns correct JSON")

    asyncio.run(_run())


def test_tool_invalid_command(tool):
    """Test tool rejects invalid commands."""

    async def _run():
        result = await tool.ainvoke({
            "comando": "invalid_cmd",
            "solicitud": "test",
            "extra": "",
        })
        assert "Comando inválido" in result
        assert "invalid_cmd" in result
        print("  [PASS] Invalid command rejected with error message")

    asyncio.run(_run())


def test_schema_new_fields():
    """Test KapsoInboundResponse has audio/video fields."""
    from app.schemas.kapso import KapsoInboundResponse
    from app.schemas.chat import TimingInfo

    fields = KapsoInboundResponse.model_fields
    assert "audio_url" in fields, "Missing audio_url field"
    assert "audio_caption" in fields, "Missing audio_caption field"
    assert "video_url" in fields, "Missing video_url field"
    assert "video_caption" in fields, "Missing video_caption field"
    print("  [PASS] KapsoInboundResponse has audio_url, audio_caption, video_url, video_caption")

    # Test serialization with new fields
    resp = KapsoInboundResponse(
        reply_type="audio",
        reply_text="Te envío un audio",
        audio_url="https://example.com/audio.mp3",
        audio_caption="Escucha esto",
        recipient_phone="14705500109",
        phone_number_id="pn_123",
        message_id="msg_456",
        conversation_id="conv_789",
        agent_id=1,
        agent_name="Test Agent",
        model_used="test-model",
        timing=TimingInfo(total_ms=100),
    )
    data = resp.model_dump()
    assert data["reply_type"] == "audio"
    assert data["audio_url"] == "https://example.com/audio.mp3"
    assert data["audio_caption"] == "Escucha esto"
    assert data["video_url"] is None
    assert data["video_caption"] is None
    print("  [PASS] KapsoInboundResponse serializes correctly with audio fields")

    # Test video
    resp2 = KapsoInboundResponse(
        reply_type="video",
        reply_text="Mira este video",
        video_url="https://example.com/video.mp4",
        video_caption="Tutorial paso a paso",
        recipient_phone="14705500109",
        phone_number_id="pn_123",
        message_id="msg_456",
        conversation_id="conv_789",
        agent_id=1,
        agent_name="Test Agent",
        model_used="test-model",
        timing=TimingInfo(total_ms=100),
    )
    data2 = resp2.model_dump()
    assert data2["reply_type"] == "video"
    assert data2["video_url"] == "https://example.com/video.mp4"
    print("  [PASS] KapsoInboundResponse serializes correctly with video fields")


def test_comando_detection_logic():
    """Test the comando detection logic that kapso_routes uses."""
    from app.schemas.chat import ToolCall

    # Simulate merged_tools with an ejecutar_comando result
    tool_output_image = json.dumps({
        "__comando__": True,
        "comando": "image",
        "solicitud": "https://cdn.example.com/img.png",
        "extra": "Mira esta foto",
        "contacto_id": 42,
    })

    merged_tools = [
        ToolCall(
            tool_name="guardar_nota",
            tool_input={"nota": "test"},
            tool_output="ok",
            duration_ms=10,
        ),
        ToolCall(
            tool_name="ejecutar_comando",
            tool_input={"comando": "image", "solicitud": "https://cdn.example.com/img.png", "extra": "Mira esta foto"},
            tool_output=tool_output_image,
            duration_ms=5,
        ),
        ToolCall(
            tool_name="send_reaction",
            tool_input={"emoji": "🔥"},
            tool_output="reaction:🔥",
            duration_ms=1,
        ),
    ]

    # Replicate the detection logic from kapso_routes
    reaction_emoji = None
    comando_data = None
    for tc in merged_tools:
        if tc.tool_name == "send_reaction" and tc.tool_input.get("emoji"):
            reaction_emoji = tc.tool_input["emoji"]
        if tc.tool_name == "ejecutar_comando" and tc.tool_output:
            try:
                _parsed = json.loads(tc.tool_output)
                if isinstance(_parsed, dict) and _parsed.get("__comando__"):
                    comando_data = _parsed
            except (json.JSONDecodeError, TypeError):
                pass

    assert reaction_emoji == "🔥", f"Expected 🔥, got {reaction_emoji}"
    assert comando_data is not None, "comando_data should be detected"
    assert comando_data["comando"] == "image"
    assert comando_data["solicitud"] == "https://cdn.example.com/img.png"
    assert comando_data["extra"] == "Mira esta foto"
    print("  [PASS] Comando detection extracts both reaction_emoji and comando_data")

    # Test reply_type resolution
    reply_type = "text"
    image_url = None
    image_caption = None
    audio_url = None
    audio_caption = None
    video_url = None
    video_caption = None
    final_reply_text = "Aquí tienes la imagen"

    if comando_data:
        cmd = comando_data.get("comando", "")
        cmd_url = comando_data.get("solicitud", "")
        cmd_extra = comando_data.get("extra", "")
        if cmd == "image" and cmd_url:
            reply_type = "image"
            image_url = cmd_url
            image_caption = cmd_extra or final_reply_text
        elif cmd == "audio" and cmd_url:
            reply_type = "audio"
            audio_url = cmd_url
            audio_caption = cmd_extra or None
        elif cmd == "video" and cmd_url:
            reply_type = "video"
            video_url = cmd_url
            video_caption = cmd_extra or final_reply_text

    assert reply_type == "image"
    assert image_url == "https://cdn.example.com/img.png"
    assert image_caption == "Mira esta foto"
    assert audio_url is None
    assert video_url is None
    print("  [PASS] Reply type resolved to 'image' with correct URL and caption")


def test_comando_invalid_output():
    """Test graceful handling of malformed tool output."""
    from app.schemas.chat import ToolCall

    merged_tools = [
        ToolCall(
            tool_name="ejecutar_comando",
            tool_input={"comando": "bad"},
            tool_output="❌ Comando inválido: 'bad'. Comandos válidos: audio, image, monica, video",
            duration_ms=1,
        ),
    ]

    comando_data = None
    for tc in merged_tools:
        if tc.tool_name == "ejecutar_comando" and tc.tool_output:
            try:
                _parsed = json.loads(tc.tool_output)
                if isinstance(_parsed, dict) and _parsed.get("__comando__"):
                    comando_data = _parsed
            except (json.JSONDecodeError, TypeError):
                pass

    assert comando_data is None, "Invalid command output should not produce comando_data"
    print("  [PASS] Invalid command output gracefully ignored (no comando_data)")


if __name__ == "__main__":
    print("\n=== Test: Tool Factory ===")
    tool = test_tool_factory()

    print("\n=== Test: Valid Commands ===")
    test_tool_valid_commands(tool)

    print("\n=== Test: Invalid Command ===")
    test_tool_invalid_command(tool)

    print("\n=== Test: Schema New Fields ===")
    test_schema_new_fields()

    print("\n=== Test: Comando Detection Logic ===")
    test_comando_detection_logic()

    print("\n=== Test: Invalid Output Handling ===")
    test_comando_invalid_output()

    print("\n✅ ALL TESTS PASSED\n")
