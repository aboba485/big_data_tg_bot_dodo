const button = document.querySelector("#submit");
const message = document.querySelector("#message");
const result = document.querySelector("#result");

function setMessage(text, kind = "") {
  message.hidden = !text;
  message.className = `message ${kind}`;
  message.textContent = text;
}

button.addEventListener("click", async () => {
  button.disabled = true;
  button.textContent = "Формируем…";
  result.hidden = true;
  setMessage("");
  try {
    const response = await fetch("/api/reports", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        query: document.querySelector("#query").value,
        output_format: document.querySelector("#format").value,
      }),
    });
    const data = await response.json();
    if (!response.ok || data.status === "error") {
      throw new Error(data.error?.message || "Не удалось сформировать отчёт");
    }
    if (data.status === "needs_clarification") {
      setMessage(data.question, "warning");
      return;
    }
    if (data.status === "unsupported") {
      setMessage(data.reason, "warning");
      return;
    }
    document.querySelector("#meta").textContent =
      `${data.summary} Вызовов Dodo IS: ${data.execution.dodo_requests_count}. ` +
      `OpenAI tokens: ${data.execution.openai_input_tokens}/${data.execution.openai_output_tokens}.`;
    const head = document.querySelector("thead");
    const body = document.querySelector("tbody");
    head.replaceChildren();
    body.replaceChildren();
    const header = document.createElement("tr");
    data.columns.forEach(column => {
      const cell = document.createElement("th");
      cell.textContent = column;
      header.appendChild(cell);
    });
    head.appendChild(header);
    data.rows.forEach(row => {
      const tr = document.createElement("tr");
      data.columns.forEach(column => {
        const td = document.createElement("td");
        td.textContent = row[column] ?? "—";
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    document.querySelector("#totals").textContent =
      `Итого: ${JSON.stringify(data.totals, null, 2)}`;
    const link = document.querySelector("#download");
    link.hidden = !data.download;
    if (data.download) link.href = data.download.url;
    result.hidden = false;
  } catch (error) {
    setMessage(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Сформировать отчёт";
  }
});

