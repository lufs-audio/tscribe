import sys
import argparse

def main() -> int:
    parser = argparse.ArgumentParser(description="tscribe CLI")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args()

    if args.json:
        import json
        print(json.dumps({"status": "success", "message": "Hello from tscribe!"}))
    else:
        print("Hello from tscribe!")

    return 0

if __name__ == "__main__":
    sys.exit(main())
