import json
import time

from app.rag.relevance import check_relevance
from app.rag.retriever import search


QUESTIONS_PATH = "app/evaluation/questions.json"

TOP_K = 2
MIN_SIMILARITY = 0.50


def load_questions():
    with open(
        QUESTIONS_PATH,
        "r",
        encoding="utf-8"
    ) as file:
        return json.load(file)


def filter_relevant_results(question, retrieved_results):
    relevant_results = []

    for result in retrieved_results:
        context = result["metadata"]["text"]

        relevance = check_relevance(
            question,
            context
        )

        if relevance == "RELEVANT":
            relevant_results.append(result)

    return relevant_results


def contains_expected_text(results, expected_text):
    return any(
        expected_text in result["metadata"]["text"]
        for result in results
    )


def evaluate():
    questions = load_questions()

    stats = {
        "direct": {"total": 0, "correct": 0},
        "paraphrased": {"total": 0, "correct": 0},
        "unanswerable": {"total": 0, "correct": 0}
    }

    latencies = []

    print(
        f"Loaded {len(questions)} evaluation questions."
    )

    print(
        "\n=== RAG Relevance Validation Evaluation ==="
    )

    for item in questions:
        start_time = time.perf_counter()

        retrieved_results = search(
            item["question"],
            top_k=TOP_K,
            min_similarity=MIN_SIMILARITY
        )

        relevant_results = filter_relevant_results(
            item["question"],
            retrieved_results
        )

        latency = time.perf_counter() - start_time
        latencies.append(latency)

        category = item["category"]
        stats[category]["total"] += 1

        if item["answerable"]:
            correct = contains_expected_text(
                relevant_results,
                item["expected_text"]
            )

            if correct:
                stats[category]["correct"] += 1
            else:
                print(
                    f"\n[FAIL] {item['id']}"
                )
                print(
                    f"Question: {item['question']}"
                )
                print(
                    f"Expected: {item['expected_text']}"
                )

        else:
            if not relevant_results:
                stats[category]["correct"] += 1
            else:
                print(
                    f"\n[FALSE POSITIVE] {item['id']}"
                )
                print(
                    f"Question: {item['question']}"
                )

                for result in relevant_results:
                    metadata = result["metadata"]

                    print(
                        f"  Chunk {metadata['chunk_id']} "
                        f"(score={result['score']:.4f})"
                    )

        print(
            f"{item['id']} "
            f"| retrieved={len(retrieved_results)} "
            f"| relevant={len(relevant_results)} "
            f"| latency={latency:.2f}s"
        )

    print("\n=== Evaluation Summary ===")

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
            metric = "Rejection accuracy"
        else:
            metric = "Recall@2 after validation"

        print(
            f"{category.capitalize():15s} "
            f"{metric:28s} "
            f"{accuracy:.2%} "
            f"({correct}/{total})"
        )

    average_latency = (
        sum(latencies) / len(latencies)
        if latencies
        else 0
    )

    print(
        f"\nAverage retrieval + validation latency: "
        f"{average_latency:.2f}s"
    )


if __name__ == "__main__":
    evaluate()