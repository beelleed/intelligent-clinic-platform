// Adapted from https://github.com/beelleed/intelligent-clinic-platform — the live UI uses the session-scoped chat stream.

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
let externalSearchAvailable = false;

function updateResourceVisibility() {
    document.getElementById("official-resources").classList.toggle("hidden", !publicChatEnabled || !externalSearchAvailable);
}

// Render a small, safe subset of answer formatting; never interpret model HTML.
function renderAnswer(text) {
    answerElement.replaceChildren();
    let list = null;
    for (const line of text.split(/\r?\n/)) {
        if (!line.trim()) { list = null; continue; }
        const item = line.match(/^\s*[-*]\s+(.+)$/);
        if (item) {
            if (!list) { list = document.createElement("ul"); answerElement.append(list); }
            const li = document.createElement("li");
            li.textContent = item[1];
            list.append(li);
        } else {
            list = null;
            const paragraph = document.createElement("p");
            paragraph.textContent = line;
            if (line.startsWith("External reference: https://")) {
                try {
                    const url = new URL(line.slice("External reference: ".length).trim());
                    const domains = ["cdc.gov", "nih.gov", "medlineplus.gov", "who.int", "fda.gov", "hhs.gov"];
                    if (url.protocol === "https:" && !url.username && !url.password && domains.some((domain) => url.hostname === domain || url.hostname.endsWith(`.${domain}`))) {
                        const link = document.createElement("a");
                        link.href = url.href;
                        link.textContent = `Official source · ${url.hostname} ↗`;
                        link.target = "_blank";
                        link.rel = "noopener noreferrer";
                        paragraph.replaceChildren(link);
                    }
                } catch (_error) { /* Invalid URLs remain inert plain text. */ }
            }
            if (line.startsWith("[Source:")) paragraph.className = "source-note";
            answerElement.append(paragraph);
        }
    }
}


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
    document.getElementById("official-resources-button").disabled = isLoading;
    exampleButtons.forEach((button) => {
        button.disabled = isLoading;
    });
    document.querySelectorAll(".visit-shortcut").forEach((button) => {
        button.disabled = isLoading;
    });
}


function setPublicDemoMode() {
    publicChatEnabled = false;
    updateResourceVisibility();
    modeNotice.classList.remove("hidden");
    sessionStrip.classList.add("hidden");
    resetButton.classList.add("hidden");
    chatForm.classList.add("hidden");
    document.getElementById("chat-heading").textContent = "How can we help you today?";
    modeDescription.textContent = (
        "Choose a topic for a quick answer. For schedules and appointment "
        + "information, ask Lumi."
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
    updateResourceVisibility();
    modeNotice.classList.add("hidden");
    sessionStrip.classList.remove("hidden");
    resetButton.classList.remove("hidden");
    chatForm.classList.remove("hidden");
    document.getElementById("chat-heading").textContent = "Chat with Lumi";
    modeDescription.textContent = (
        "Tell Lumi what's on your mind about your visit. Your clinic guide "
        + "checks sources and can follow along with your questions."
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


async function readChatStream(response) {
    if (!response.body) {
        throw new Error("Your browser could not read Lumi's response stream.");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let eventName = "";
    let dataLines = [];
    let completed = false;
    let answered = false;

    function dispatchEvent() {
        if (!dataLines.length) {
            eventName = "";
            return;
        }
        const data = JSON.parse(dataLines.join("\n"));
        if (eventName === "progress" && typeof data.message === "string") {
            loadingDescription.textContent = data.message;
        } else if (eventName === "answer" && typeof data.response === "string") {
            if (!data.response.trim()) {
                throw new Error("Lumi returned an invalid response.");
            }
            renderAnswer(data.response);
            answerSection.classList.remove("hidden");
            answered = true;
        } else if (eventName === "error") {
            throw new Error(data.message || "Lumi could not complete the request.");
        } else if (eventName === "done") {
            completed = true;
        }
        eventName = "";
        dataLines = [];
    }

    try {
        while (true) {
            const {value, done} = await reader.read();
            buffer += decoder.decode(value, {stream: !done});
            let lineEnd;
            while ((lineEnd = buffer.indexOf("\n")) !== -1) {
                const line = buffer.slice(0, lineEnd).replace(/\r$/, "");
                buffer = buffer.slice(lineEnd + 1);
                if (!line) {
                    dispatchEvent();
                } else if (line.startsWith("event:")) {
                    eventName = line.slice(6).trim();
                } else if (line.startsWith("data:")) {
                    dataLines.push(line.slice(5).trimStart());
                }
            }
            if (done) {
                break;
            }
        }
    } finally {
        reader.releaseLock();
    }

    if (!completed || !answered) {
        throw new Error("Lumi's response was interrupted. Please try again.");
    }
}


async function askAgent(query) {
    clearMessages();
    setLoading(true);

    try {
        const response = await fetch("/chat/stream", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Accept": "text/event-stream"
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

        await readChatStream(response);
    } catch (error) {
        answerSection.classList.add("hidden");
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

        renderAnswer(formatDemoResponse(data));
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
            document.getElementById("shortcut-guidance").textContent = "Lumi is currently unavailable. You can still browse all clinic FAQs below.";
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
        exampleButtons.forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
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

fetch("/capabilities").then((response) => response.ok ? response.json() : {}).then((data) => {
    externalSearchAvailable = data.official_resources === true;
    updateResourceVisibility();
}).catch(() => {});

document.getElementById("official-resources-button").addEventListener("click", () => {
    if (!publicChatEnabled || !externalSearchAvailable) return;
    questionInput.value = "Search external official CDC websites for public handwashing information and give me the source links.";
    questionInput.scrollIntoView({block: "center"});
    questionInput.focus({preventScroll: true});
});

document.querySelectorAll(".visit-shortcut").forEach((button) => {
    button.addEventListener("click", () => {
        clearMessages();
        if (!liveAgentAvailable) {
            setPublicDemoMode();
            document.getElementById("chat-heading").scrollIntoView({block: "center"});
            showError("Lumi is currently unavailable. Please browse the clinic FAQs instead.");
            return;
        }
        setLiveAgentMode();
        questionInput.value = button.dataset.visitQuery;
        questionInput.scrollIntoView({block: "center"});
        questionInput.focus({preventScroll: true});
    });
});

// Maps results stay in this panel, never in clinic conversation memory.
fetch("/maps/status").then((response) => response.json()).then((status) => {
    document.getElementById("maps-panel").classList.toggle("hidden", !status.configured);
}).catch(() => {});

document.getElementById("maps-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = document.getElementById("maps-button");
    const result = document.getElementById("maps-result");
    button.disabled = true;
    result.textContent = "Checking Google Maps…";
    try {
        const response = await fetch("/maps/search", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({query: document.getElementById("maps-query").value.trim()})
        });
        if (!response.ok) throw new Error(await parseError(response));
        const data = await response.json();
        result.replaceChildren();
        const summary = document.createElement("p");
        summary.textContent = data.summary;
        result.append(summary);
        for (const source of data.sources) {
            const url = new URL(source.url);
            if (url.protocol !== "https:" || !["www.google.com", "maps.google.com", "maps.google.co.uk", "maps.app.goo.gl"].includes(url.hostname)) continue;
            const link = document.createElement("a");
            link.href = url.href;
            link.textContent = `[${source.index}] ${source.title} · Google Maps ↗`;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            result.append(link);
        }
    } catch (error) { result.textContent = error.message || "Location lookup unavailable."; }
    finally { button.disabled = false; }
});
