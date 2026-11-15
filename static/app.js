// Adapted from https://github.com/beelleed/intelligent-clinic-platform — the live UI uses the session-scoped chat stream.

const SESSION_STORAGE_KEY = "clinic-agent-session-id";

const chatForm = document.getElementById("chat-form");
const questionInput = document.getElementById("question");
const askButton = document.getElementById("ask-button");
const resetButton = document.getElementById("reset-button");
const demoModeButton = document.getElementById("demo-mode-button");
const modeNotice = document.getElementById("mode-notice");
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
const resultCards = document.getElementById("result-cards");
const bookingDialog = document.getElementById("booking-dialog");
const confirmBookingButton = document.getElementById("confirm-booking");
let selectedAppointment = null;
let bookingInFlight = false;
let activeClinicAction = null;
let doctors = [];
let interfaceBusy = false;
const clinicPanel = document.getElementById("clinic-panel");
const specialtySelect = document.getElementById("clinic-specialty");
const doctorSelect = document.getElementById("clinic-doctor");
const clinicDate = document.getElementById("clinic-date");
const myAppointmentsButton = document.getElementById("my-appointments-button");
const cancelReservationDialog = document.getElementById("cancel-reservation-dialog");
const confirmCancellationButton = document.getElementById("confirm-cancellation");
let selectedCancellation = null;
let cancellationInFlight = false;

async function showMyAppointments() {
    if (interfaceBusy || !liveAgentAvailable) return;
    clearMessages();
    clinicPanel.classList.add("hidden");
    activeClinicAction = null;
    myAppointmentsButton.setAttribute("aria-pressed", "true");
    document.querySelectorAll("[data-clinic-action]").forEach(button => button.setAttribute("aria-pressed", "false"));
    loadingTitle.textContent = "Loading your reservations…";
    loadingDescription.textContent = "Checking reservations saved for this browser.";
    setLoading(true);
    try {
        const response = await fetch("/appointments/mine", {cache: "no-store"});
        if (!response.ok) throw new Error(await parseError(response));
        const data = await response.json();
        answerHeading.textContent = "MY APPOINTMENTS";
        groundingNote.textContent = "Saved for this browser. Clearing site data removes access. Demo data may reset when the hosted service restarts.";
        renderCards(data.cards);
        renderAnswer(data.response);
        answerSection.classList.remove("hidden");
        answerSection.scrollIntoView({block: "start", behavior: "smooth"});
    } catch (error) {
        showError(error instanceof Error ? error.message : "Your reservations are unavailable. Please try again.");
    } finally { setLoading(false); }
}

myAppointmentsButton.addEventListener("click", showMyAppointments);
document.getElementById("keep-reservation").addEventListener("click", () => cancelReservationDialog.close());
cancelReservationDialog.addEventListener("close", () => { selectedCancellation = null; });
cancelReservationDialog.addEventListener("cancel", event => { if (cancellationInFlight) event.preventDefault(); });
confirmCancellationButton.addEventListener("click", async () => {
    if (!selectedCancellation || cancellationInFlight) return;
    const reservation = selectedCancellation;
    cancellationInFlight = true;
    cancelReservationDialog.querySelectorAll("button").forEach(button => { button.disabled = true; });
    confirmCancellationButton.textContent = "Cancelling…";
    setLoading(true);
    errorSection.classList.add("hidden");
    try {
        const response = await fetch("/appointments/cancel", {method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({booking_reference: reservation.booking_reference})});
        if (!response.ok) throw new Error(await parseError(response));
        const data = await response.json();
        answerHeading.textContent = "RESERVATION CANCELLED";
        groundingNote.textContent = "Demo reservation · The released time can now be selected again.";
        renderCards(data.cards);
        renderAnswer(data.response);
        answerSection.classList.remove("hidden");
    } catch (error) {
        showError(error instanceof Error ? error.message : "Cancellation could not be confirmed. Refresh My appointments.");
    } finally {
        cancellationInFlight = false;
        cancelReservationDialog.close();
        cancelReservationDialog.querySelectorAll("button").forEach(button => { button.disabled = false; });
        confirmCancellationButton.textContent = "Cancel reservation";
        setLoading(false);
    }
});

function renderOperationCard(card) {
    const article = document.createElement("article");
    article.className = "appointment-card operation-card";
    const data = card.data;
    article.append(cardText("span", card.kind === "queue" ? "CURRENT QUEUE SNAPSHOT" : "RECURRING SCHEDULE", "result-label"),
        cardText("h3", data.doctor_name));
    if (card.kind === "queue") {
        article.append(cardText("p", "Now serving", "card-notice"), cardText("p", data.current_number, "queue-number"),
            cardText("p", `Last issued number: ${data.last_issued_number}`),
            cardText("p", `Updated: ${data.updated_at} · ${data.timezone}`, "card-notice"));
    } else if (Array.isArray(data.schedule) && data.schedule.length) {
        const table = document.createElement("table");
        const caption = cardText("caption", `Regular clinic hours for ${data.doctor_name}`);
        table.append(caption);
        const head = document.createElement("thead");
        const heading = document.createElement("tr");
        for (const label of ["Day", "Time", "Location"]) {
            const cell = cardText("th", label);
            cell.scope = "col";
            heading.append(cell);
        }
        head.append(heading);
        const body = document.createElement("tbody");
        for (const entry of data.schedule) {
            const row = document.createElement("tr");
            for (const value of [entry.weekday, `${entry.start_time} – ${entry.end_time}`, entry.location]) row.append(cardText("td", value));
            body.append(row);
        }
        table.append(head, body);
        const wrapper = document.createElement("div");
        wrapper.className = "schedule-table";
        wrapper.append(table);
        article.append(wrapper);
    } else article.append(cardText("p", "No recurring hours were returned for this doctor."));
    article.append(cardText("p", `Source: ${card.source}`, "source-note"));
    return article;
}

function populateDoctors() {
    const previous = doctorSelect.value;
    doctorSelect.replaceChildren();
    if (activeClinicAction === "appointments" && !specialtySelect.value) doctorSelect.append(new Option("All doctors", ""));
    for (const doctor of doctors.filter(item => !specialtySelect.value || item.specialty === specialtySelect.value)) {
        doctorSelect.append(new Option(doctor.name, doctor.id));
    }
    if (Array.from(doctorSelect.options).some(option => option.value === previous)) doctorSelect.value = previous;
}

async function runClinicLookup() {
    clearMessages();
    loadingTitle.textContent = "Checking clinic information…";
    loadingDescription.textContent = "Retrieving the latest available clinic data.";
    setLoading(true);
    try {
        const response = await fetch("/clinic/lookup", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({action: activeClinicAction, session_id: getSessionId(),
                doctor_id: doctorSelect.value || null,
                appointment_date: activeClinicAction === "appointments" && clinicDate.value ? clinicDate.value : null})
        });
        if (!response.ok) throw new Error(await parseError(response));
        const data = await response.json();
        if (activeClinicAction === "appointments") {
            clinicDate.replaceChildren();
            for (const day of data.available_dates || []) clinicDate.append(new Option(day, day));
            if (!clinicDate.options.length) clinicDate.append(new Option("No available dates", ""));
            else if (Array.from(clinicDate.options).some(option => option.value === data.selected_date)) clinicDate.value = data.selected_date;
            else {
                clinicDate.prepend(new Option("Select another available date", ""));
                clinicDate.value = "";
            }
        }
        answerHeading.textContent = {appointments: "AVAILABLE APPOINTMENTS", schedule: "DOCTOR SCHEDULE", queue: "QUEUE SNAPSHOT"}[activeClinicAction];
        groundingNote.textContent = "Synthetic clinic data · No patient information is collected.";
        renderCards(data.cards);
        renderAnswer(data.response);
        answerSection.classList.remove("hidden");
        answerSection.scrollIntoView({block: "nearest", behavior: "smooth"});
    } catch (error) {
        showError(error instanceof Error ? error.message : "Clinic information is unavailable. Please try again.");
    } finally { setLoading(false); }
}

async function openClinicAction(action) {
    if (interfaceBusy || !liveAgentAvailable) return;
    activeClinicAction = action;
    myAppointmentsButton.setAttribute("aria-pressed", "false");
    clearMessages();
    clinicPanel.classList.remove("hidden");
    document.querySelectorAll("[data-clinic-action]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.clinicAction === action)));
    document.getElementById("clinic-panel-heading").textContent = {appointments: "Find an appointment", schedule: "Doctor schedules", queue: "Queue numbers"}[action];
    document.getElementById("clinic-panel-description").textContent = action === "appointments"
        ? "Choose a specialty, doctor and available date. Then select a time below."
        : "Choose a doctor to view their information. You can refresh the result below.";
    document.getElementById("clinic-date-group").classList.toggle("hidden", action !== "appointments");
    document.getElementById("clinic-lookup-button").textContent = {appointments: "Refresh times", schedule: "Show schedule", queue: "Refresh queue"}[action];
    loadingTitle.textContent = "Loading doctors…";
    loadingDescription.textContent = "Retrieving the clinic directory.";
    setLoading(true);
    try {
        if (!doctors.length) {
            const response = await fetch("/clinic/doctors");
            if (!response.ok) throw new Error(await parseError(response));
            const data = await response.json();
            if (!Array.isArray(data.doctors) || !data.doctors.length) throw new Error("No doctors are available. Please try again later.");
            doctors = data.doctors;
            specialtySelect.replaceChildren(new Option("All specialties", ""));
            for (const specialty of [...new Set(doctors.map(doctor => doctor.specialty))]) specialtySelect.append(new Option(specialty, specialty));
        }
        populateDoctors();
        clinicDate.value = "";
        await runClinicLookup();
    } catch (error) {
        showError(error instanceof Error ? error.message : "Unable to load doctors. Please try again.");
    } finally {
        setLoading(false);
        clinicPanel.scrollIntoView({block: "start", behavior: "smooth"});
        document.getElementById("clinic-panel-heading").focus({preventScroll: true});
    }
}

document.querySelectorAll("[data-clinic-action]").forEach(button => button.addEventListener("click", () => openClinicAction(button.dataset.clinicAction)));
specialtySelect.addEventListener("change", () => {
    populateDoctors();
    clinicDate.value = "";
    runClinicLookup();
});
doctorSelect.addEventListener("change", () => {
    clinicDate.value = "";
    runClinicLookup();
});
clinicDate.addEventListener("change", runClinicLookup);
document.getElementById("clinic-lookup-form").addEventListener("submit", event => {
    event.preventDefault();
    if (interfaceBusy) return;
    if (!doctors.length) openClinicAction(activeClinicAction);
    else runClinicLookup();
});

function cardText(tag, text, className) {
    const element = document.createElement(tag);
    element.textContent = text;
    if (className) element.className = className;
    return element;
}

function appointmentDetails(slot) {
    const details = document.createElement("dl");
    for (const [label, value] of [["Doctor", slot.doctor_name], ["Date", slot.appointment_date],
        ["Time", `${slot.start_time} – ${slot.end_time}`], ["Timezone", slot.timezone]]) {
        const row = document.createElement("div");
        row.append(cardText("dt", label), cardText("dd", value));
        details.append(row);
    }
    return details;
}

function renderCards(cards) {
    resultCards.replaceChildren();
    if (!Array.isArray(cards)) return;
    const groups = new Map();
    for (const card of cards.slice(0, 48)) {
        if (card.kind !== "appointment_slot" || !card.slot || typeof card.selection_token !== "string") continue;
        const key = `${card.slot.doctor_id}:${card.slot.appointment_date}:${card.slot.timezone}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(card);
    }
    for (const group of groups.values()) {
        const slot = group[0].slot;
        const article = document.createElement("article");
        article.className = "appointment-card slot-group";
        article.append(cardText("span", "CHOOSE A TIME", "result-label"), cardText("h3", slot.doctor_name),
            cardText("p", `${slot.appointment_date} · ${slot.timezone}`, "slot-date"));
        const times = document.createElement("div");
        times.className = "time-slots";
        for (const card of group) {
            const button = cardText("button", card.slot.start_time, "time-slot");
            button.type = "button";
            button.disabled = interfaceBusy;
            button.setAttribute("aria-label", `${card.slot.doctor_name}, ${card.slot.appointment_date}, ${card.slot.start_time} to ${card.slot.end_time}, ${card.slot.timezone}`);
            button.title = `${card.slot.start_time} – ${card.slot.end_time}`;
            button.addEventListener("click", () => {
                if (interfaceBusy) return;
                selectedAppointment = card;
                document.getElementById("booking-summary").replaceChildren(appointmentDetails(card.slot));
                bookingDialog.showModal();
                confirmBookingButton.focus();
            });
            times.append(button);
        }
        article.append(times, cardText("p", "Select a time to review. A reservation is made only after confirmation.", "card-notice"),
            cardText("p", `Source: ${group[0].source}`, "source-note"));
        resultCards.append(article);
    }
    for (const card of cards) {
        if (["schedule", "queue"].includes(card.kind) && card.data) {
            resultCards.append(renderOperationCard(card));
            continue;
        }
        if (card.kind !== "reservation" || !card.slot) continue;
        const status = card.status || "upcoming";
        const article = document.createElement("article");
        article.className = `appointment-card reserved${status !== "upcoming" ? " archived-reservation" : ""}`;
        article.append(cardText("span", {upcoming: "DEMO RESERVATION CONFIRMED", past: "PAST RESERVATION", cancelled: "CANCELLED"}[status] || "DEMO RESERVATION", "result-label"),
            cardText("h3", status === "cancelled" ? "Reservation cancelled." : card.slot.doctor_name), appointmentDetails(card.slot));
        article.append(cardText("p", `Booking reference: ${card.booking_reference}`, "booking-reference"));
        if (card.can_cancel) {
            const cancel = cardText("button", "Cancel reservation", "secondary-button cancel-reservation-button");
            cancel.type = "button";
            cancel.disabled = interfaceBusy;
            cancel.addEventListener("click", () => {
                if (interfaceBusy) return;
                selectedCancellation = card;
                document.getElementById("cancel-reservation-summary").replaceChildren(
                    appointmentDetails(card.slot), cardText("p", `Reference: ${card.booking_reference}`, "booking-reference"));
                cancelReservationDialog.showModal();
                document.getElementById("keep-reservation").focus();
            });
            article.append(cancel);
        }
        if (!card.status || status === "cancelled") {
            const view = cardText("button", "View my appointments", "secondary-button");
            view.type = "button";
            view.addEventListener("click", showMyAppointments);
            article.append(view);
        }
        article.append(cardText("p", `Source: ${card.source}`, "source-note"));
        resultCards.append(article);
    }
}

document.getElementById("cancel-booking").addEventListener("click", () => bookingDialog.close());
bookingDialog.addEventListener("close", () => { selectedAppointment = null; });
bookingDialog.addEventListener("cancel", (event) => { if (bookingInFlight) event.preventDefault(); });
confirmBookingButton.addEventListener("click", async () => {
    if (!selectedAppointment || bookingInFlight) return;
    const selection = selectedAppointment;
    bookingInFlight = true;
    bookingDialog.querySelectorAll("button").forEach(button => { button.disabled = true; });
    confirmBookingButton.textContent = "Reserving…";
    setLoading(true);
    errorSection.classList.add("hidden");
    try {
        const response = await fetch("/appointments/confirm", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({session_id: getSessionId(), selection_token: selection.selection_token})
        });
        if (!response.ok) throw new Error(await parseError(response));
        const data = await response.json();
        answerHeading.textContent = "DEMO RESERVATION";
        groundingNote.textContent = "Synthetic demo reservation · No patient information was collected.";
        renderAnswer(data.response);
        renderCards(data.cards);
        answerSection.classList.remove("hidden");
    } catch (error) {
        // Never retry a write automatically: the response may have been lost.
        resultCards.replaceChildren();
        showError(error instanceof Error ? error.message : "Unable to confirm this reservation. Check availability before trying again.");
    } finally {
        bookingInFlight = false;
        bookingDialog.close();
        bookingDialog.querySelectorAll("button").forEach(button => { button.disabled = false; });
        confirmBookingButton.textContent = "Confirm demo reservation";
        setLoading(false);
        answerSection.scrollIntoView({block: "nearest", behavior: "smooth"});
    }
});

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
    interfaceBusy = isLoading;
    loadingSection.classList.toggle("hidden", !isLoading);
    askButton.disabled = isLoading;
    resetButton.disabled = isLoading;
    demoModeButton.disabled = isLoading || !liveAgentAvailable;
    myAppointmentsButton.disabled = isLoading || !liveAgentAvailable;
    document.getElementById("official-resources-button").disabled = isLoading;
    exampleButtons.forEach((button) => {
        button.disabled = isLoading;
    });
    document.querySelectorAll(".appointment-card button, #clinic-lookup-form select, #clinic-lookup-form input, #clinic-lookup-button").forEach(button => { button.disabled = isLoading; });
    document.querySelectorAll("[data-clinic-action]").forEach(button => { button.disabled = isLoading || !liveAgentAvailable; });
}


function setPublicDemoMode() {
    publicChatEnabled = false;
    document.querySelector(".demo-buttons").classList.remove("hidden");
    updateResourceVisibility();
    modeNotice.classList.remove("hidden");
    sessionStrip.classList.add("hidden");
    resetButton.classList.add("hidden");
    chatForm.classList.add("hidden");
    document.getElementById("lumi-description").textContent = "Lumi is currently unavailable. All clinic FAQs remain available here.";
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
        demoModeButton.disabled = true;
        demoModeButton.textContent = "Lumi unavailable";
    }
}


function setLiveAgentMode() {
    if (!liveAgentAvailable) {
        return;
    }
    publicChatEnabled = true;
    updateResourceVisibility();
    modeNotice.classList.remove("hidden");
    sessionStrip.classList.remove("hidden");
    resetButton.classList.remove("hidden");
    chatForm.classList.remove("hidden");
    document.getElementById("lumi-description").textContent = "Have a follow-up or a question that isn't covered here? Chat with Lumi.";
    loadingTitle.textContent = "Lumi is working...";
    loadingDescription.textContent = (
        "Reasoning, selecting tools, and preparing a response."
    );
    answerHeading.textContent = "LUMI RESPONSE";
    groundingNote.textContent = (
        "Lumi provides sample clinic operations information and does not "
        + "provide medical advice."
    );
    demoModeButton.textContent = "Ask Lumi";
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
    resultCards.replaceChildren();
    if (bookingDialog.open && !bookingInFlight) bookingDialog.close();
    if (cancelReservationDialog.open && !cancellationInFlight) cancelReservationDialog.close();
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
            renderCards(data.cards);
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
    if (interfaceBusy) return;
    clearMessages();
    setLiveAgentMode();
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
        answerSection.scrollIntoView({block: "start", behavior: "smooth"});
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
    if (interfaceBusy) return;
    clearMessages();
    loadingTitle.textContent = "Loading FAQ response...";
    loadingDescription.textContent = "Retrieving a predefined clinic FAQ response.";
    answerHeading.textContent = "FAQ ANSWER";
    groundingNote.textContent = "Sample clinic FAQ information only. These answers do not provide medical advice.";
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
        answerSection.scrollIntoView({block: "start", behavior: "smooth"});
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
            setLiveAgentMode();
        }
        document.querySelectorAll("[data-clinic-action]").forEach(button => { button.disabled = !liveAgentAvailable; });
        myAppointmentsButton.disabled = !liveAgentAvailable;
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
        runDemo(button.dataset.demoId, button.dataset.question);
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
    document.getElementById("chat-heading").scrollIntoView({block: "start", behavior: "smooth"});
});


demoModeButton.addEventListener("click", () => {
    if (!publicChatEnabled) return;
    questionInput.scrollIntoView({block: "center", behavior: "smooth"});
    questionInput.focus({preventScroll: true});
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
