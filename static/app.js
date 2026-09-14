const questionInput =
    document.getElementById("question");

const askButton =
    document.getElementById("ask-button");

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

    askButton.disabled = true;
}


function hideLoading() {
    loadingSection.classList.add("hidden");
    askButton.disabled = false;
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

        empty.textContent =
            "No relevant source found.";

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

        title.textContent =
            source.source;

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


async function askQuestion() {
    const question =
        questionInput.value.trim();

    if (!question) {
        showError("Please enter a question.");
        return;
    }

    showLoading();

    try {
        const response =
            await fetch("/query", {
                method: "POST",
                headers: {
                    "Content-Type":
                        "application/json"
                },
                body: JSON.stringify({
                    question: question
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

        answerSection.classList.remove(
            "hidden"
        );

        errorSection.classList.add(
            "hidden"
        );

    } catch (error) {
        console.error(error);

        showError(
            "Unable to process the question. Please try again."
        );
    } finally {
        hideLoading();
    }
}


askButton.addEventListener(
    "click",
    askQuestion
);


exampleButtons.forEach((button) => {
    button.addEventListener(
        "click",
        () => {
            questionInput.value =
                button.dataset.question;

            questionInput.focus();
        }
    );
});


questionInput.addEventListener(
    "keydown",
    (event) => {
        if (
            (event.ctrlKey || event.metaKey) &&
            event.key === "Enter"
        ) {
            askQuestion();
        }
    }
);