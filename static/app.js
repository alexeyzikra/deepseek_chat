"use strict";

const state = {
  personas: [],
  personaId: null,
  persona: null,
  chats: [],
  chatId: null,
  chat: null,
  messages: [],
  busy: false,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function fail(error) {
  alert(error.message || String(error));
}

/* ------------------------------------------------------------- запуск */

async function boot() {
  const data = await api("/api/bootstrap");
  state.personas = data.personas;
  state.personaId = data.current_persona_id;

  if (!state.personaId) {
    $("setup").classList.remove("hidden");
    $("setup-name").focus();
    return;
  }
  $("app").classList.remove("hidden");
  await loadPersona();
  await loadChats();
}

async function loadPersona() {
  state.persona = await api(`/api/personas/${state.personaId}`);
  $("persona-name").textContent = state.persona.name;
  $("persona-avatar").textContent = [...state.persona.name][0]?.toUpperCase() || "?";
}

/* -------------------------------------------------------------- чаты */

function groupOf(isoDate) {
  const days = (Date.now() - new Date(isoDate).getTime()) / 86400000;
  if (days < 1) return "Сегодня";
  if (days < 7) return "7 дней";
  if (days < 30) return "30 дней";
  return "Давно";
}

async function loadChats() {
  state.chats = await api(`/api/personas/${state.personaId}/chats`);
  renderChats();
  if (state.chats.length) {
    const stillThere = state.chats.some((c) => c.id === state.chatId);
    await openChat(stillThere ? state.chatId : state.chats[0].id);
  } else {
    state.chatId = null;
    state.chat = null;
    state.messages = [];
    $("messages").replaceChildren();
    $("chat-title").textContent = "";
    $("chat-meta").textContent = "";
    $("system-block").classList.add("hidden");
    updateComposer();
  }
}

function renderChats() {
  const list = $("chat-list");
  list.replaceChildren();
  let lastGroup = null;
  const pinned = state.chats.filter((c) => c.pinned);
  const rest = state.chats.filter((c) => !c.pinned);

  const addItem = (chat) => {
    const row = document.createElement("div");
    row.className = "chat-row" + (chat.id === state.chatId ? " active" : "");
    row.dataset.id = chat.id;

    const button = document.createElement("button");
    button.className = "chat-item";
    if (chat.pinned) {
      const pin = document.createElement("span");
      pin.className = "pin";
      pin.textContent = "📌";
      button.appendChild(pin);
    }
    button.appendChild(document.createTextNode(chat.title));
    button.onclick = () => openChat(chat.id).catch(fail);

    const more = document.createElement("button");
    more.className = "chat-more";
    more.textContent = "⋯";
    more.title = "Действия с чатом";
    more.onclick = (event) => {
      event.stopPropagation();
      openChatMenu(chat, more);
    };

    row.append(button, more);
    list.appendChild(row);
  };

  if (pinned.length) {
    const header = document.createElement("div");
    header.className = "chat-group";
    header.textContent = "Закреплённые";
    list.appendChild(header);
    pinned.forEach(addItem);
    lastGroup = "__pinned__";
  }
  for (const chat of rest) {
    const group = groupOf(chat.created_at);
    if (group !== lastGroup) {
      const header = document.createElement("div");
      header.className = "chat-group";
      header.textContent = group;
      list.appendChild(header);
      lastGroup = group;
    }
    addItem(chat);
  }
}

async function openChat(id, keepScroll = false) {
  const data = await api(`/api/chats/${id}`);
  state.chatId = id;
  state.chat = data.chat;
  state.messages = data.messages;
  renderChats();

  $("chat-title").textContent = data.chat.title;
  if (data.chat.forked_from_chat_id) {
    const source = state.chats.find((c) => c.id === data.chat.forked_from_chat_id);
    const note = document.createElement("span");
    note.className = "fork-note";
    note.textContent = source ? ` · форк из ${source.title}` : " · форк";
    $("chat-title").appendChild(note);
  }
  updateMeta(data.spent_millicents, data.context_tokens, data.context_limit);
  renderSystemPrompt();
  renderMessages();
  if (!keepScroll) scrollDown(true);
}

function updateMeta(spent, tokens, limit) {
  const parts = [];
  if (tokens) {
    parts.push(`контекст ${tokens.toLocaleString("ru")} / ${(limit / 1e6).toFixed(0)}M`);
  }
  parts.push(`потрачено ${formatCost(spent)}`);
  $("chat-meta").textContent = parts.join(" · ");
}

function formatCost(millicents) {
  if (!millicents) return "0";
  if (millicents < 10) return `${millicents.toFixed(2)} мц`;
  if (millicents < 1000) return `${Math.round(millicents)} мц`;
  if (millicents < 100000) return `${(millicents / 1000).toFixed(2)} ¢`;
  return `$${(millicents / 100000).toFixed(2)}`;
}

/* ------------------------------------------------- системный промпт */

function renderSystemPrompt() {
  const block = $("system-block");
  block.classList.remove("hidden");
  const view = $("system-view");
  view.replaceChildren();

  const text = state.chat.system_prompt || "";
  const pre = document.createElement("pre");
  pre.textContent = text.trim() || "— пусто —";
  const edit = document.createElement("button");
  edit.className = "link-button";
  edit.textContent = "Изменить";
  edit.onclick = () => editSystemPrompt();
  view.append(pre, edit);
}

function editSystemPrompt() {
  const view = $("system-view");
  $("system-block").open = true;
  const editor = buildEditor(
    state.chat.system_prompt || "",
    "Действует только на этот чат. Промпт личности и другие чаты не меняются.",
    async (value) => {
      await api(`/api/chats/${state.chatId}`, {
        method: "PATCH",
        body: JSON.stringify({ system_prompt: value }),
      });
      state.chat.system_prompt = value;
      renderSystemPrompt();
    },
    renderSystemPrompt
  );
  view.replaceChildren(editor);
  editor.querySelector("textarea").focus();
}

/* --------------------------------------------------------- сообщения */

function renderBody(element, text) {
  element.replaceChildren();
  const parts = text.split("```");
  parts.forEach((part, index) => {
    if (index % 2 === 1) {
      const pre = document.createElement("pre");
      const newline = part.indexOf("\n");
      pre.textContent = newline >= 0 ? part.slice(newline + 1) : part;
      element.appendChild(pre);
    } else if (part) {
      element.appendChild(document.createTextNode(part));
    }
  });
}

function reasoningElement(text) {
  const details = document.createElement("details");
  details.className = "reasoning";
  const summary = document.createElement("summary");
  summary.textContent = "Размышление";
  const content = document.createElement("div");
  content.textContent = text;
  details.append(summary, content);
  return details;
}

function renderMessages() {
  const container = $("messages");
  container.replaceChildren();
  state.messages.forEach((message, index) => {
    container.appendChild(messageElement(message, index));
  });
  updateComposer();
}

function messageElement(message, index) {
  const wrapper = document.createElement("div");
  wrapper.className = "msg msg-" + message.role;
  wrapper.dataset.id = message.id;

  if (message.reasoning_content) {
    wrapper.appendChild(reasoningElement(message.reasoning_content));
  }

  const body = document.createElement("div");
  body.className = "body";
  renderBody(body, message.content);
  wrapper.appendChild(body);

  if (message.cost_millicents) {
    const cost = document.createElement("div");
    cost.className = "cost";
    cost.textContent = formatCost(message.cost_millicents);
    wrapper.appendChild(cost);
  }

  wrapper.appendChild(actionsRow(message, index, wrapper));
  return wrapper;
}

function actionsRow(message, index, wrapper) {
  const row = document.createElement("div");
  row.className = "actions";

  const add = (label, handler, danger) => {
    const button = document.createElement("button");
    button.className = "action" + (danger ? " danger" : "");
    button.textContent = label;
    button.onclick = handler;
    row.appendChild(button);
    return button;
  };

  add("копировать", async () => {
    try {
      await navigator.clipboard.writeText(message.content);
    } catch (_) {}
  });

  add("изменить", () => startEdit(message, wrapper));

  if (message.role === "user") {
    add("перегенерировать", () => regenerate(message, index));
  } else {
    add("форк", () => forkFrom(message, index));
  }

  // Удаляется это сообщение и весь хвост после него.
  const tail = state.messages.length - index;
  add(
    "удалить",
    async () => {
      const question =
        tail > 1
          ? `Удалить это сообщение и ещё ${tail - 1} после него? Отменить нельзя.`
          : "Удалить это сообщение?";
      if (!confirm(question)) return;
      try {
        await api(`/api/messages/${message.id}`, { method: "DELETE" });
        await openChat(state.chatId, true);
      } catch (error) {
        fail(error);
      }
    },
    true
  );

  return row;
}

/* ------------------------------------------------------------ правка */

function buildEditor(value, hint, onSave, onCancel) {
  const box = document.createElement("div");
  box.className = "editor";

  const area = document.createElement("textarea");
  area.value = value;
  box.appendChild(area);

  const buttons = document.createElement("div");
  buttons.className = "editor-buttons";

  const save = document.createElement("button");
  save.className = "save";
  save.textContent = "Сохранить";
  save.onclick = async () => {
    save.disabled = true;
    try {
      await onSave(area.value);
    } catch (error) {
      fail(error);
      save.disabled = false;
    }
  };

  const cancel = document.createElement("button");
  cancel.textContent = "Отмена";
  cancel.onclick = () => onCancel();

  buttons.append(save, cancel);
  box.appendChild(buttons);

  if (hint) {
    const note = document.createElement("div");
    note.className = "editor-hint";
    note.textContent = hint;
    box.appendChild(note);
  }

  area.style.height = "auto";
  setTimeout(() => {
    area.style.height = Math.max(area.scrollHeight, 90) + "px";
  }, 0);
  return box;
}

function startEdit(message, wrapper) {
  const hint =
    message.role === "assistant"
      ? "Это попадёт в историю как его собственный ответ. Ничего не перегенерируется."
      : "Меняется только история. Чтобы получить новый ответ — перегенерируй.";

  const editor = buildEditor(
    message.content,
    hint,
    async (value) => {
      await api(`/api/messages/${message.id}`, {
        method: "PATCH",
        body: JSON.stringify({ content: value }),
      });
      await openChat(state.chatId, true);
    },
    () => renderMessages()
  );
  wrapper.replaceChildren(editor);
  editor.querySelector("textarea").focus();
}

/* ------------------------------------------------------- генерация */

function pendingAssistantElement() {
  const wrapper = document.createElement("div");
  wrapper.className = "msg msg-assistant";
  const reasoning = reasoningElement("");
  reasoning.classList.add("hidden");
  const body = document.createElement("div");
  body.className = "body";
  wrapper.append(reasoning, body);
  return wrapper;
}

async function streamInto(url, payload, wrapper) {
  const reasoning = wrapper.querySelector(".reasoning");
  const body = wrapper.querySelector(".body");
  let contentText = "";
  let reasoningText = "";

  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload ? JSON.stringify(payload) : null,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let split;
    while ((split = buffer.indexOf("\n\n")) >= 0) {
      const raw = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      if (!raw.startsWith("data: ")) continue;
      const event = JSON.parse(raw.slice(6));

      if (event.type === "reasoning") {
        reasoningText += event.text;
        reasoning.classList.remove("hidden");
        reasoning.querySelector("div").textContent = reasoningText;
        scrollDown(false);
      } else if (event.type === "content") {
        contentText += event.text;
        renderBody(body, contentText);
        scrollDown(false);
      } else if (event.type === "error") {
        throw new Error(event.message);
      } else if (event.type === "done") {
        return event;
      }
    }
  }
  throw new Error("Соединение оборвалось, ответ не сохранён");
}

async function runGeneration(url, payload, placeAfter) {
  if (state.busy || !state.chatId) return;
  state.busy = true;
  updateComposer();

  const wrapper = pendingAssistantElement();
  if (placeAfter) placeAfter(wrapper);
  else $("messages").appendChild(wrapper);
  scrollDown(true);

  try {
    const done = await streamInto(url, payload, wrapper);
    updateMeta(done.chat_spent_millicents, done.context_tokens, 1_000_000);
    state.busy = false;
    await openChat(state.chatId, true);
  } catch (error) {
    wrapper.classList.add("msg-error");
    wrapper.querySelector(".body").textContent = "Ошибка: " + error.message;
    state.busy = false;
    // Ничего не записалось и ничего не стёрлось — история осталась как была.
    setTimeout(() => openChat(state.chatId, true).catch(fail), 2500);
  } finally {
    state.busy = false;
    updateComposer();
  }
}

async function send() {
  const input = $("input");
  const text = input.value.trim();
  if (!text || state.busy || !state.chatId) return;
  input.value = "";
  resizeInput();

  $("messages").appendChild(
    messageElement({ id: 0, role: "user", content: text }, state.messages.length)
  );
  await runGeneration(`/api/chats/${state.chatId}/send`, { content: text });
}

async function regenerate(message, index) {
  const tail = state.messages.length - index - 1;
  if (tail > 1) {
    const question = `Перегенерация сотрёт ${tail} сообщений после этого. Отменить нельзя. Продолжить?`;
    if (!confirm(question)) return;
  }
  // Хвост убираем с экрана сразу, но на сервере он доживёт до удачного ответа.
  const container = $("messages");
  while (container.children.length > index + 1) {
    container.lastElementChild.remove();
  }
  await runGeneration(`/api/messages/${message.id}/regenerate`, null);
}

/* ------------------------------------------- низ экрана и черновики */

function updateComposer() {
  const last = state.messages[state.messages.length - 1];
  const waitingForBot = last && last.role === "user";
  const hasChat = Boolean(state.chatId);

  $("send").disabled = state.busy || !hasChat || waitingForBot;
  $("append-user").disabled = state.busy || !hasChat || waitingForBot;
  $("input").disabled = !hasChat || waitingForBot;

  const controls = $("tail-controls");
  controls.replaceChildren();
  controls.classList.toggle("hidden", !waitingForBot || state.busy);
  if (!waitingForBot || state.busy) return;

  const note = document.createElement("span");
  note.textContent = "Твоё сообщение в истории, ответа ещё нет:";
  controls.appendChild(note);

  const ask = document.createElement("button");
  ask.textContent = "Получить ответ";
  ask.onclick = () => regenerate(last, state.messages.length - 1);

  const fake = document.createElement("button");
  fake.textContent = "Ответить за бота";
  fake.onclick = () => draftAssistant();

  controls.append(ask, fake);
}

function draftAssistant() {
  const draft = document.createElement("div");
  draft.className = "draft";
  const label = document.createElement("div");
  label.className = "label";
  label.textContent = "Ответ от лица бота — в API не уходит, денег не стоит";
  const editor = buildEditor(
    "",
    "Дальше он увидит это как свой собственный ответ.",
    async (value) => {
      if (!value.trim()) {
        draft.remove();
        updateComposer();
        return;
      }
      await api(`/api/chats/${state.chatId}/messages`, {
        method: "POST",
        body: JSON.stringify({ role: "assistant", content: value }),
      });
      draft.remove();
      await openChat(state.chatId, true);
    },
    () => {
      draft.remove();
      updateComposer();
    }
  );
  draft.append(label, editor);
  $("messages").appendChild(draft);
  $("tail-controls").classList.add("hidden");
  editor.querySelector("textarea").focus();
  scrollDown(true);
}

async function appendUser() {
  const input = $("input");
  const text = input.value.trim();
  if (!text || state.busy || !state.chatId) return;
  try {
    await api(`/api/chats/${state.chatId}/messages`, {
      method: "POST",
      body: JSON.stringify({ role: "user", content: text }),
    });
    input.value = "";
    resizeInput();
    await openChat(state.chatId, true);
  } catch (error) {
    fail(error);
  }
}

function scrollDown(force) {
  const scroll = $("scroll");
  const nearBottom =
    scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 150;
  if (force || nearBottom) scroll.scrollTop = scroll.scrollHeight;
}

/* -------------------------------------------- форк и действия с чатом */

async function forkFrom(message, index) {
  const kept = index + 1;
  const tail = state.messages.length - kept;
  const question =
    tail > 0
      ? `Сделать копию чата с первыми ${kept} сообщениями?\n\nПоследние ${tail} в копию не войдут. Этот чат останется как есть.`
      : "Сделать полную копию этого чата?";
  if (!confirm(question)) return;
  try {
    const created = await api(`/api/messages/${message.id}/fork`, { method: "POST" });
    state.chatId = created.id;
    await loadChats();
  } catch (error) {
    fail(error);
  }
}

async function refreshChatList() {
  state.chats = await api(`/api/personas/${state.personaId}/chats`);
  renderChats();
  const current = state.chats.find((c) => c.id === state.chatId);
  if (current) $("chat-title").textContent = current.title;
}

function openChatMenu(chat, anchor) {
  const menu = $("persona-menu");
  menu.replaceChildren();

  const item = (label, handler, danger) => {
    const button = document.createElement("button");
    if (danger) button.className = "danger";
    button.textContent = label;
    button.onclick = () => {
      closeMenu();
      handler();
    };
    menu.appendChild(button);
  };

  item("Переименовать", () => startRename(chat));
  item(chat.pinned ? "Открепить" : "Закрепить", () => togglePin(chat));

  const separator = document.createElement("div");
  separator.className = "sep";
  menu.appendChild(separator);
  item("Удалить", () => removeChat(chat), true);

  const box = anchor.getBoundingClientRect();
  menu.classList.remove("hidden");
  menu.style.bottom = "";
  menu.style.left = box.left + "px";
  menu.style.top = box.bottom + 6 + "px";
  $("overlay").classList.remove("hidden");
}

function startRename(chat) {
  const row = $("chat-list").querySelector(`.chat-row[data-id="${chat.id}"]`);
  if (!row) return;

  const input = document.createElement("input");
  input.className = "rename-input";
  input.value = chat.title;
  row.replaceChildren(input);
  input.focus();
  input.select();

  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    settled = true;
    const title = input.value.trim();
    if (!save || !title || title === chat.title) return renderChats();
    try {
      await api(`/api/chats/${chat.id}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      });
      await refreshChatList();
    } catch (error) {
      fail(error);
      renderChats();
    }
  };

  input.onkeydown = (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      finish(true);
    } else if (event.key === "Escape") {
      event.preventDefault();
      finish(false);
    }
  };
  input.onblur = () => finish(true);
}

async function togglePin(chat) {
  try {
    await api(`/api/chats/${chat.id}`, {
      method: "PATCH",
      body: JSON.stringify({ pinned: !chat.pinned }),
    });
    await refreshChatList();
  } catch (error) {
    fail(error);
  }
}

async function removeChat(chat) {
  const question =
    `Удалить чат «${chat.title}» со всеми сообщениями?\n\n` +
    "Восстановить его будет нельзя — данные стираются из файла базы.\n" +
    "Расходы по нему останутся в статистике, но без текста.";
  if (!confirm(question)) return;
  try {
    await api(`/api/chats/${chat.id}`, { method: "DELETE" });
    if (state.chatId === chat.id) state.chatId = null;
    await loadChats();
  } catch (error) {
    fail(error);
  }
}

/* ------------------------------------------------------------- меню */

function closeMenu() {
  $("persona-menu").classList.add("hidden");
  $("overlay").classList.add("hidden");
}

function openPersonaMenu() {
  const menu = $("persona-menu");
  menu.replaceChildren();

  for (const persona of state.personas) {
    const button = document.createElement("button");
    button.textContent = persona.name;
    if (persona.id === state.personaId) button.classList.add("current");
    button.onclick = async () => {
      closeMenu();
      if (persona.id === state.personaId) return;
      await api("/api/current-persona", {
        method: "POST",
        body: JSON.stringify({ persona_id: persona.id }),
      });
      state.personaId = persona.id;
      state.chatId = null;
      await loadPersona();
      await loadChats();
    };
    menu.appendChild(button);
  }

  const separator = document.createElement("div");
  separator.className = "sep";
  menu.appendChild(separator);

  const create = document.createElement("button");
  create.textContent = "Создать личность";
  create.onclick = () => {
    closeMenu();
    $("app").classList.add("hidden");
    $("setup").classList.remove("hidden");
    $("setup-name").value = "";
    $("setup-prompt").value = "";
    $("setup-thinking").checked = false;
    $("setup-name").focus();
  };
  menu.appendChild(create);

  const anchor = $("persona-button").getBoundingClientRect();
  menu.classList.remove("hidden");
  menu.style.top = "";
  menu.style.left = anchor.left + "px";
  menu.style.bottom = window.innerHeight - anchor.top + 8 + "px";
  $("overlay").classList.remove("hidden");
}

/* --------------------------------------------------------- мелочи UI */

function resizeInput() {
  const input = $("input");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 220) + "px";
}

$("setup-create").onclick = async () => {
  const name = $("setup-name").value.trim();
  const errorBox = $("setup-error");
  if (!name) {
    errorBox.textContent = "Впиши имя";
    errorBox.classList.remove("hidden");
    return;
  }
  errorBox.classList.add("hidden");
  try {
    const created = await api("/api/personas", {
      method: "POST",
      body: JSON.stringify({
        name,
        system_prompt: $("setup-prompt").value,
        thinking_enabled: $("setup-thinking").checked,
      }),
    });
    const data = await api("/api/bootstrap");
    state.personas = data.personas;
    state.personaId = created.id;
    state.chatId = null;
    $("setup").classList.add("hidden");
    $("app").classList.remove("hidden");
    await loadPersona();
    await loadChats();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.classList.remove("hidden");
  }
};

$("new-chat").onclick = async () => {
  try {
    const chat = await api(`/api/personas/${state.personaId}/chats`, { method: "POST" });
    state.chatId = chat.id;
    await loadChats();
    $("input").focus();
  } catch (error) {
    fail(error);
  }
};

$("toggle-sidebar").onclick = () => $("app").classList.toggle("collapsed");
$("persona-button").onclick = openPersonaMenu;
$("overlay").onclick = closeMenu;
$("send").onclick = () => send().catch(fail);
$("append-user").onclick = () => appendUser().catch(fail);

$("input").addEventListener("input", resizeInput);
$("input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    send().catch(fail);
  }
});

boot().catch((error) => {
  document.body.textContent = "Не удалось запуститься: " + error.message;
});
