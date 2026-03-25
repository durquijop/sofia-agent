"""Quick test for the closing followup fix."""
from app.agents.conversational import _is_closing_followup

tests = [
    ("Si en proseso", False),
    ("Con visa", False),
    ("Ok gracias", False),
    ("Si", False),
    ("Dale", False),
    ("Listo", False),
    ("\U0001f44d", False),
    ("Chao", True),
    ("Bye", True),
    ("Adios", True),
    ("Buenas noches", True),
    ("Hasta luego", True),
    ("Saludos", True),
    ("Cuídate", True),
    ("Necesito ayuda", False),
    ("Tengo una visa B1", False),
    ("Gracias", False),
    ("Perfecto", False),
    ("Claro", False),
]

passed = 0
for msg, expected in tests:
    result = _is_closing_followup(msg)
    status = "PASS" if result == expected else "FAIL"
    if result != expected:
        print(f"  {status} '{msg}' -> got {result}, expected {expected}")
    else:
        passed += 1

print(f"\n{passed}/{len(tests)} tests passed")
if passed == len(tests):
    print("ALL TESTS PASSED")
else:
    print("SOME TESTS FAILED")
