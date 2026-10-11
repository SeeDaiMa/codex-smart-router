"""Keep Codex's bundled base instructions when generating the Auto model catalog."""

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", default="codex")
    parser.add_argument("--template-model", default="gpt-6.1-sol")
    args = parser.parse_args()
    result = subprocess.run(
        [args.cli, "--no-daemon", "debug", "models", "--bundled"],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    value = json.loads(result.stdout)
    models = value["models"] if isinstance(value, dict) else value
    template = next((item for item in models if item["slug"] == args.template_model), None)
    if template is None or not template.get("base_instructions"):
        raise SystemExit("No bundled base instructions for the selected template model")
    path = ROOT / "config" / "codex-models.json"
    catalog = json.loads(path.read_text())
    catalog["models"][0]["base_instructions"] = template["base_instructions"]
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")
    print(f"Preserved bundled base instructions from {args.template_model} in {path}")


if __name__ == "__main__":
    main()
