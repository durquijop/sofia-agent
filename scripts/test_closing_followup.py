"""Test closing followup detection logic."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.conversational import _is_closing_followup, _infer_closing_emoji, CLOSING_FOLLOWUP_MARKER


def test_closing_detection():
    """Messages that SHOULD be detected as closing followup."""
    closing_messages = [
        "ok",
        "Ok",
        "OK",
        "okey",
        "dale",
        "listo",
        "perfecto",
        "gracias",
        "muchas gracias",
        "bye",
        "chao",
        "adios",
        "nos vemos",
        "buenas noches",
        "👍",
        "❤️",
        "🙏",
        "👋",
        "😊",
        "🔥",
        "si",
        "sip",
        "claro",
        "entendido",
        "de acuerdo",
        "sale",
        "va",
        "un abrazo",
        "bendiciones",
        "que descanses",
        "genial",
        "excelente",
    ]
    passed = 0
    failed = 0
    for msg in closing_messages:
        result = _is_closing_followup(msg)
        if result:
            passed += 1
        else:
            print(f"  [FAIL] Should be closing: '{msg}'")
            failed += 1
    print(f"  Closing detection: {passed}/{len(closing_messages)} passed, {failed} failed")
    return failed


def test_not_closing():
    """Messages that should NOT be detected as closing followup."""
    not_closing = [
        "Quiero información sobre la visa",
        "Cuánto cuesta el servicio?",
        "Necesito agendar una cita",
        "Tengo una duda sobre el proceso",
        "Me pueden ayudar con un problema?",
        "Hola buenas tardes quiero saber sobre las vacantes",
        "Cuándo es mi cita?",
        "Por favor llámame",
        "Necesito hablar con un asesor",
        "Cuál es el precio del paquete completo?",
        "Gracias pero tengo otra pregunta sobre el trabajo",
        "Ok pero cuándo me llaman?",
    ]
    passed = 0
    failed = 0
    for msg in not_closing:
        result = _is_closing_followup(msg)
        if not result:
            passed += 1
        else:
            print(f"  [FAIL] Should NOT be closing: '{msg}'")
            failed += 1
    print(f"  Not-closing detection: {passed}/{len(not_closing)} passed, {failed} failed")
    return failed


def test_emoji_selection():
    """Test emoji inference for closing messages."""
    cases = [
        ("gracias", "🙏"),
        ("muchas gracias", "🙏"),
        ("bye", "👋"),
        ("nos vemos", "👋"),
        ("buenas noches", "👋"),
        ("perfecto", "🔥"),
        ("excelente", "🔥"),
        ("ok", "👍"),
        ("dale", "👍"),
        ("un abrazo", "❤️"),
    ]
    passed = 0
    for msg, expected in cases:
        result = _infer_closing_emoji(msg)
        if result == expected:
            passed += 1
        else:
            print(f"  [FAIL] '{msg}' → expected {expected}, got {result}")
    print(f"  Emoji selection: {passed}/{len(cases)} passed")
    return len(cases) - passed


def test_marker():
    assert CLOSING_FOLLOWUP_MARKER == "__closing_followup__"
    print("  [PASS] Marker constant is correct")
    return 0


if __name__ == "__main__":
    total_failures = 0
    print("\n=== Test: Closing Followup Detection ===")
    total_failures += test_closing_detection()

    print("\n=== Test: NOT Closing (Business Messages) ===")
    total_failures += test_not_closing()

    print("\n=== Test: Emoji Selection ===")
    total_failures += test_emoji_selection()

    print("\n=== Test: Marker Constant ===")
    total_failures += test_marker()

    if total_failures == 0:
        print("\n✅ ALL TESTS PASSED\n")
    else:
        print(f"\n❌ {total_failures} FAILURES\n")
        sys.exit(1)
