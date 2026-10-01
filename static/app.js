// Adapted from https://github.com/beelleed/intelligent-clinic-platform — replaced the earlier demo behavior with the required /chat API and session memory.

const SESSION_STORAGE_KEY = "clinic-agent-session-id";

const chatForm = document.getElementById("chat-form");
const questionInput = document.getElementById("question");
const askButton = document.getElementById("ask-button");
const resetButton = document.getElementById("reset-button");
const demoModeButton = document.getElementById("demo-mode-button");
const modeNotice = document.getElementById("mode-notice");
const modeDescription = document.getElementById("mode-description");
const sessionStrip = document.getElementById("session-strip");
const sessionIdElement = document.getElementById("session-id");
const loadingSection = document.getElementById("loading");
const loadingTitle = document.getElementById("loading-title");
const loadingDescription = document.getElementById("loading-description");
const answerSection = document.getElementById("answer-section");
const answerHeading = document.getElementById("answer-heading");
const answerElement = document.getElementById("answer");
const demoQuestionGroup = document.getElementById("demo-question-group");
const demoQuestionElement = document.getElementById("demo-question");
const groundingNote = document.getElementById("grounding-note");
const errorSection = document.getElementById("error-section");
const errorMessage = document.getElementById("error-message");
const demoFallbackButton = document.getElementById("demo-fallback-button");
const exampleButtons = document.querySelectorAll(".example-button");
let publicChatEnabled = false;
let liveAgentAvailable = true;


function createSessionId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID();
    }
    return `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}


function getSessionId() {
    let sessionId = window.localStorage.getItem(SESSION_STORAGE_KEY);
    if (!sessionId) {
        sessionId = createSessionId();
        window.localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
    }
    return sessionId;
}


function displaySessionId() {
    const sessionId = getSessionId();
    sessionIdElement.textContent = sessionId.slice(0, 12);
    sessionIdElement.title = sessionId;
}


function setLoading(isLoading) {
    loadingSection.classList.toggle("hidden", !isLoading);
    askButton.disabled = isLoading;
    resetButton.disabled = isLoading;
    demoModeButton.disabled = isLoading;
    exampleButtons.forEach((button) => {
        button.disabled = isLoading;
    });
}


function setPublicDemoMode() {
    publicChatEnabled = false;
    modeNotice.classList.remove("hidden");
    sessionStrip.classList.add("hidden");
    resetButton.classList.add("hidden");
    chatForm.classList.add("hidden");
    document.getElementById("chat-heading").textContent = "Browse clinic FAQs";
    modeDescription.textContent = (
        "Explore ten basic clinic FAQs below. Current operational information "
        + "is available only through Lumi."
    );
    loadingTitle.textContent = "Loading FAQ response...";
    loadingDescription.textContent = (
        "Retrieving a predefined clinic FAQ response."
    );
    answerHeading.textContent = "FAQ ANSWER";
    groundingNote.textContent = (
        "Sample clinic FAQ information only. These answers do not provide "
        + "medical advice."
    );
    if (liveAgentAvailable) {
        demoModeButton.textContent = "Ask Lumi";
        demoModeButton.classList.remove("hidden");
    } else {
        demoModeButton.classList.add("hidden");
    }
}


function setLiveAgentMode() {
    if (!liveAgentAvailable) {
        return;
    }
    publicChatEnabled = true;
    modeNotice.classList.add("hidden");
    sessionStrip.classList.remove("hidden");
    resetButton.classList.remove("hidden");
    chatForm.classList.remove("hidden");
    document.getElementById("chat-heading").textContent = "Chat with Lumi";
    modeDescription.textContent = (
        "Lumi is a virtual clinic guide that selects tools from three MCP "
        + "servers and remembers context within this browser session."
    );
    loadingTitle.textContent = "Lumi is working...";
    loadingDescription.textContent = (
        "Reasoning, selecting tools, and preparing a response."
    );
    answerHeading.textContent = "LUMI RESPONSE";
    groundingNote.textContent = (
        "Lumi provides sample clinic operations information and does not "
        + "provide medical advice."
    );
    demoModeButton.textContent = "Back to Clinic FAQs";
    demoModeButton.classList.remove("hidden");
}


function showError(message) {
    errorMessage.textContent = message;
    errorSection.classList.remove("hidden");
}


function showRateLimitError(message, retryAfter, reason) {
    let guidance;
    if (reason === "minute" && retryAfter) {
        guidance = `Try again in about ${retryAfter} seconds, or browse clinic FAQs.`;
    } else {
        guidance = "You can continue with the predefined clinic FAQs.";
    }
    showError(`${message} ${guidance}`);
    demoFallbackButton.classList.remove("hidden");
}


function clearMessages() {
    answerSection.classList.add("hidden");
    demoQuestionGroup.classList.add("hidden");
    errorSection.classList.add("hidden");
    answerElement.textContent = "";
    demoQuestionElement.textContent = "";
    errorMessage.textContent = "";
    demoFallbackButton.classList.add("hidden");
}


async function parseError(response) {
    try {
        const data = await response.json();
        if (typeof data.detail === "string" && data.detail.trim()) {
            return data.detail;
        }
    } catch (_error) {
        // Fall through to the controlled status message below.
    }
    return `Request failed with status ${response.status}.`;
}


async function askAgent(query) {
    clearMessages();
    setLoading(true);

    try {
        const response = await fetch("/chat", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                query,
                session_id: getSessionId()
            })
        });

        if (response.status === 429) {
            const message = await parseError(response);
            showRateLimitError(
                message,
                response.headers.get("Retry-After"),
                response.headers.get("X-Rate-Limit-Reason")
            );
            return;
        }

        if (!response.ok) {
            throw new Error(await parseError(response));
        }

        const data = await response.json();
        if (typeof data.response !== "string" || !data.response.trim()) {
            throw new Error("Lumi returned an invalid response.");
        }

        answerElement.textContent = data.response;
        answerSection.classList.remove("hidden");
    } catch (error) {
        const message = error instanceof Error
            ? error.message
            : "Unable to contact Lumi.";
        showError(message);
    } finally {
        setLoading(false);
    }
}


function formatDemoResponse(data) {
    const sources = Array.isArray(data.sources)
        ? data.sources
            .map((item) => item && item.source)
            .filter(Boolean)
        : [];
    if (!sources.length) {
        return data.answer;
    }
    return `${data.answer}\n\n[Source: ${sources.join("; ")}]`;
}


async function runDemo(demoId, question) {
    clearMessages();
    questionInput.value = question;
    setLoading(true);

    try {
        const response = await fetch("/demo/query", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({demo_id: demoId})
        });

        if (response.status === 404) {
            throw new Error(
                "The FAQ data is newer than the running server. Restart "
                + "python main.py and refresh this page."
            );
        }

        if (!response.ok) {
            throw new Error(await parseError(response));
        }

        const data = await response.json();
        if (typeof data.answer !== "string" || !data.answer.trim()) {
            throw new Error("The demo returned an invalid response.");
        }

        answerElement.textContent = formatDemoResponse(data);
        demoQuestionElement.textContent = question;
        demoQuestionGroup.classList.remove("hidden");
        answerSection.classList.remove("hidden");
    } catch (error) {
        const message = error instanceof Error
            ? error.message
            : "Unable to load the clinic FAQs.";
        showError(message);
    } finally {
        setLoading(false);
    }
}


async function configureMode() {
    try {
        const response = await fetch("/health");
        if (!response.ok) {
            return;
        }
        const health = await response.json();
        if (health.public_chat_enabled === false) {
            liveAgentAvailable = false;
            setPublicDemoMode();
        } else {
            setPublicDemoMode();
        }
    } catch (_error) {
        // Keep the key-free FAQ interface available if health cannot be read.
    }
}


chatForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = questionInput.value.trim();
    if (!query) {
        showError("Enter a question before sending the request.");
        questionInput.focus();
        return;
    }
    askAgent(query);
});


questionInput.addEventListener("keydown", (event) => {
    if (event.ctrlKey && event.key === "Enter") {
        event.preventDefault();
        chatForm.requestSubmit();
    }
});


exampleButtons.forEach((button) => {
    button.addEventListener("click", () => {
        if (!publicChatEnabled) {
            runDemo(button.dataset.demoId, button.dataset.question);
            return;
        }
        questionInput.value = button.dataset.question;
        questionInput.focus();
    });
});


resetButton.addEventListener("click", () => {
    window.localStorage.setItem(SESSION_STORAGE_KEY, createSessionId());
    questionInput.value = "";
    clearMessages();
    displaySessionId();
    questionInput.focus();
});


demoFallbackButton.addEventListener("click", () => {
    clearMessages();
    questionInput.value = "";
    setPublicDemoMode();
});


demoModeButton.addEventListener("click", () => {
    clearMessages();
    questionInput.value = "";
    if (publicChatEnabled) {
        setPublicDemoMode();
    } else {
        setLiveAgentMode();
    }
});


displaySessionId();
configureMode();
