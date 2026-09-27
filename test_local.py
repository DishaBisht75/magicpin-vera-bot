"""
test_local.py — sanity-check the composer against the 30 canonical test
pairs, without needing to run a server or the judge_simulator.

Usage (from the challenge zip root, after running dataset/generate_dataset.py):
    cp /path/to/build/composer.py .
    python3 test_local.py --dataset expanded
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from composer import compose  # noqa: E402


def load_json(path):
    with open(path) as f:
        return json.load(f)


def build_index(dataset_dir, subdir, id_key):
    idx = {}
    folder = os.path.join(dataset_dir, subdir)
    for fn in os.listdir(folder):
        if not fn.endswith(".json"):
            continue
        obj = load_json(os.path.join(folder, fn))
        idx[obj[id_key]] = obj
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="expanded")
    args = ap.parse_args()

    categories = {}
    for fn in os.listdir(os.path.join(args.dataset, "categories")):
        cat = load_json(os.path.join(args.dataset, "categories", fn))
        categories[cat["slug"]] = cat

    merchants = build_index(args.dataset, "merchants", "merchant_id")
    customers = build_index(args.dataset, "customers", "customer_id")
    triggers = build_index(args.dataset, "triggers", "id")

    pairs = load_json(os.path.join(args.dataset, "test_pairs.json"))["pairs"]

    results = []
    for pair in pairs:
        trigger = triggers.get(pair["trigger_id"])
        merchant = merchants.get(pair["merchant_id"])
        customer = customers.get(pair["customer_id"]) if pair.get("customer_id") else None
        if not trigger or not merchant:
            print(f"[{pair['test_id']}] MISSING trigger or merchant — skipping")
            continue
        category = categories.get(merchant["category_slug"])
        composed = compose(category, merchant, trigger, customer)

        line = {"test_id": pair["test_id"], **composed}
        results.append(line)

        print(f"\n=== {pair['test_id']} | {merchant['identity']['name']} | "
              f"trigger={trigger['kind']} | cta={composed['cta']} | "
              f"send_as={composed['send_as']} ===")
        print(composed["body"])

    out_path = os.path.join(args.dataset, "..", "submission.jsonl")
    with open(out_path, "w") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n\nWrote {len(results)} lines to {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
