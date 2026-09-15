const questionInput =
    document.getElementById("question");

const loadingSection =
    document.getElementById("loading");

const answerSection =
    document.getElementById("answer-section");

const answerElement =
    document.getElementById("answer");

const sourcesElement =
    document.getElementById("sources");

const errorSection =
    document.getElementById("error-section");

const errorMessage =
    document.getElementById("error-message");

const exampleButtons =
    document.querySelectorAll(".example-button");


function showLoading() {
    loadingSection.classList.remove("hidden");
    answerSection.classList.add("hidden");
    errorSection.classList.add("hidden");
}


function hideLoading() {
    loadingSection.classList.add("hidden");
}


function showError(message) {
    errorMessage.textContent = message;
    errorSection.classList.remove("hidden");
}


function renderSources(sources) {
    sourcesElement.innerHTML = "";

    if (!sources || sources.length === 0) {
        const empty = document.createElement("div");

        empty.className = "source";
        empty.textContent = "No relevant source found.";

        sourcesElement.appendChild(empty);

        return;
    }

    sources.forEach((source) => {
        const container =
            document.createElement("div");

        container.className = "source";

        const title =
            document.createElement("div");

        title.className = "source-title";
        title.textContent = source.source;

        const meta =
            document.createElement("div");

        meta.className = "source-meta";

        meta.textContent =
            `Chunk ${source.chunk_id} · ` +
            `Similarity ${source.score}`;

        container.appendChild(title);
        container.appendChild(meta);

        sourcesElement.appendChild(container);
    });
}


async function runDemo(demoId, question) {
    showLoading();

    questionInput.value = question;
    errorSection.classList.add("hidden");

    try {
        const response =
            await fetch("/demo/query", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    demo_id: demoId
                })
            });

        if (!response.ok) {
            throw new Error(
                `Request failed with status ${response.status}.`
            );
        }

        const data =
            await response.json();

        answerElement.textContent =
            data.answer;

        renderSources(data.sources);

        answerSection.classList.remove("hidden");

    } catch (error) {
        console.error(error);

        showError(
            "Unable to run the demo. Please try again."
        );
    } finally {
        hideLoading();
    }
}


exampleButtons.forEach((button) => {
    button.addEventListener("click", () => {
        const demoId =
            button.dataset.demoId;

        const question =
            button.dataset.question;

        document
            .querySelectorAll(".example-button")
            .forEach((item) => {
                item.classList.remove("selected");
            });

        button.classList.add("selected");

        runDemo(
            demoId,
            question
        );
    });
});