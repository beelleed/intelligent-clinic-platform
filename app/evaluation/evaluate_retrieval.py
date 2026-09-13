import json

from app.rag.retriever import search


QUESTIONS_PATH = "app/evaluation/questions.json"
THRESHOLDS = [0.50]


def load_questions():
    with open(
        QUESTIONS_PATH,
        "r",
        encoding="utf-8"
    ) as file:
        return json.load(file)


def evaluate_threshold(threshold, questions):
    category_stats = {
        "direct": {"total": 0, "correct": 0},
        "paraphrased": {"total": 0, "correct": 0},
        "unanswerable": {"total": 0, "correct": 0}
    }

    for item in questions:
        results = search(
            item["question"],
            top_k=2,
            min_similarity=threshold
        )

        category = item["category"]
        category_stats[category]["total"] += 1

        if item["answerable"]:
            expected_text = item["expected_text"]

            retrieved_texts = [
                result["metadata"]["text"]
                for result in results
            ]

            found_expected_text = any(
                expected_text in retrieved_text
                for retrieved_text in retrieved_texts
            )

            if found_expected_text:
                category_stats[category]["correct"] += 1

            else:
                print(
                    f"\n[FAIL] {item['id']} "
                    f"(threshold={threshold:.2f})"
                )
                print(f"Question: {item['question']}")
                print(f"Expected text: {expected_text}")

                print("Retrieved:")

                for result in results:
                    metadata = result["metadata"]

                    print(
                        f"  Chunk {metadata['chunk_id']} "
                        f"(score={result['score']:.4f})"
                    )
                    print(
                        f"  {metadata['text']}"
                    )

        else:
            if not results:
                category_stats[category]["correct"] += 1

            else:
                print(
                    f"\n[FALSE POSITIVE] {item['id']} "
                    f"(threshold={threshold:.2f})"
                )
                print(f"Question: {item['question']}")

                for result in results:
                    metadata = result["metadata"]

                    print(
                        f"  Chunk {metadata['chunk_id']} "
                        f"(score={result['score']:.4f})"
                    )
                    print(
                        f"  {metadata['text']}"
                    )

    return category_stats


def print_results(threshold, stats):
    print(f"\nThreshold: {threshold:.2f}")

    for category in [
        "direct",
        "paraphrased",
        "unanswerable"
    ]:
        total = stats[category]["total"]
        correct = stats[category]["correct"]

        accuracy = (
            correct / total
            if total
            else 0
        )

        if category == "unanswerable":
            metric_name = "Rejection accuracy"
        else:
            metric_name = "Recall@2"

        print(
            f"{category.capitalize():15s} "
            f"{metric_name:18s} "
            f"{accuracy:.2%} "
            f"({correct}/{total})"
        )


if __name__ == "__main__":
    questions = load_questions()

    print(
        f"Loaded {len(questions)} evaluation questions."
    )

    print(
        "\n=== Retrieval Evaluation ==="
    )

    for threshold in THRESHOLDS:
        stats = evaluate_threshold(
            threshold,
            questions
        )

        print_results(
            threshold,
            stats
        )