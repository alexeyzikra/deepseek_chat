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
  theme: "dark",
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
  applyTheme(data.theme);
  state.personas = data.personas;
  state.personaId = data.current_persona_id;

  if (!state.personaId) {
    // Первый запуск: личностей нет, уходить отсюда некуда.
    showSetup(false);
    return;
  }
  $("app").classList.remove("hidden");
  await loadPersona();
  await loadChats();
}

/** cancellable — есть ли куда вернуться, если передумал. */
function showSetup(cancellable) {
  $("setup-name").value = "";
  $("setup-prompt").value = "";
  $("setup-thinking").checked = false;
  $("setup-error").classList.add("hidden");
  $("setup-title").textContent = cancellable
    ? "Новая личность"
    : "Создай первую личность";
  $("setup-cancel").classList.toggle("hidden", !cancellable);
  $("app").classList.add("hidden");
  $("setup").classList.remove("hidden");
  $("setup-name").focus();
}

function hideSetup() {
  if ($("setup-cancel").classList.contains("hidden")) return;
  $("setup").classList.add("hidden");
  $("app").classList.remove("hidden");
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
  if (!state.chats.length) {
    // В личности всегда должен быть открытый чат: зашёл — и пиши, без
    // отдельного «новый чат». Поэтому пустую личность заводим сразу.
    try {
      const chat = await api(`/api/personas/${state.personaId}/chats`, {
        method: "POST",
      });
      state.chatId = chat.id;
      state.chats = await api(`/api/personas/${state.personaId}/chats`);
    } catch (error) {
      fail(error);
    }
  }
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

/* --- разметка -----------------------------------------------------------
   Свой разбор, без библиотек. Текст модели попадает в страницу только
   текстовыми узлами: никакого innerHTML, поэтому разметка в ответе не
   может ничего выполнить на странице.
------------------------------------------------------------------------ */

function mdNode(name, text) {
  const node = document.createElement(name);
  node.textContent = text;
  return node;
}

function mdLink(text, href) {
  // Схемы кроме этих не открываем: ссылку предложила модель, не человек.
  if (!/^(https?:|mailto:)/i.test(href)) {
    return document.createTextNode(`${text} (${href})`);
  }
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.textContent = text;
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
  return anchor;
}

// Подчёркивание намеренно НЕ считается выделением: в разговоре о коде
// snake_case и __init__ встречаются чаще, чем курсив через _.
const INLINE_RE =
  /`([^`\n]+)`|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|~~([^~\n]+)~~|\[([^\]\n]+)\]\(([^)\s]+)\)/g;

function appendInline(parent, text) {
  let last = 0;
  let match;
  INLINE_RE.lastIndex = 0;
  while ((match = INLINE_RE.exec(text))) {
    if (match.index > last) {
      parent.appendChild(document.createTextNode(text.slice(last, match.index)));
    }
    if (match[1] !== undefined) parent.appendChild(mdNode("code", match[1]));
    else if (match[2] !== undefined) parent.appendChild(mdNode("strong", match[2]));
    else if (match[3] !== undefined) parent.appendChild(mdNode("em", match[3]));
    else if (match[4] !== undefined) parent.appendChild(mdNode("del", match[4]));
    else parent.appendChild(mdLink(match[5], match[6]));
    last = match.index + match[0].length;
  }
  if (last < text.length) {
    parent.appendChild(document.createTextNode(text.slice(last)));
  }
}

function codeBlock(code, language) {
  const wrap = document.createElement("div");
  wrap.className = "code-block";

  const head = document.createElement("div");
  head.className = "code-head";
  const label = document.createElement("span");
  label.textContent = language || "код";
  const copy = document.createElement("button");
  copy.textContent = "копировать";
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(code);
      copy.textContent = "скопировано";
      setTimeout(() => (copy.textContent = "копировать"), 1200);
    } catch (_) {}
  };
  head.append(label, copy);

  const pre = document.createElement("pre");
  pre.textContent = code;
  wrap.append(head, pre);
  return wrap;
}

const BULLET_RE = /^(\s*)([-*+])\s+(.*)$/;
const NUMBER_RE = /^(\s*)(\d+)[.)]\s+(.*)$/;

function parseMarkdown(text) {
  const lines = text.split("\n");
  const blocks = [];
  let paragraph = [];
  let i = 0;

  const flush = () => {
    if (!paragraph.length) return;
    const p = document.createElement("p");
    appendInline(p, paragraph.join("\n"));
    blocks.push(p);
    paragraph = [];
  };

  while (i < lines.length) {
    const line = lines[i];

    // Блок кода. Незакрытый тоже показываем блоком — так он выглядит
    // правильно уже во время стриминга, пока ответ ещё пишется.
    const fence = line.match(/^\s*```(.*)$/);
    if (fence) {
      flush();
      const body = [];
      i += 1;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1;
      blocks.push(codeBlock(body.join("\n"), fence[1].trim()));
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flush();
      const node = document.createElement("h" + heading[1].length);
      appendInline(node, heading[2].trim());
      blocks.push(node);
      i += 1;
      continue;
    }

    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flush();
      blocks.push(document.createElement("hr"));
      i += 1;
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      flush();
      const quoted = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quoted.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      const quote = document.createElement("blockquote");
      for (const node of parseMarkdown(quoted.join("\n"))) quote.appendChild(node);
      blocks.push(quote);
      continue;
    }

    if (BULLET_RE.test(line) || NUMBER_RE.test(line)) {
      flush();
      const ordered = !BULLET_RE.test(line);
      const list = document.createElement(ordered ? "ol" : "ul");
      while (i < lines.length) {
        const match = lines[i].match(ordered ? NUMBER_RE : BULLET_RE);
        if (!match) break;
        const item = document.createElement("li");
        appendInline(item, match[3]);
        list.appendChild(item);
        i += 1;
      }
      blocks.push(list);
      continue;
    }

    if (!line.trim()) {
      flush();
      i += 1;
      continue;
    }

    paragraph.push(line);
    i += 1;
  }

  flush();
  return blocks;
}

function renderBody(element, text, plain) {
  element.replaceChildren();
  if (plain) {
    // Своё сообщение показываем ровно как набрано: в этом инструменте
    // важно видеть, что именно лежит в истории.
    element.classList.remove("md");
    element.textContent = text;
    return;
  }
  element.classList.add("md");
  for (const node of parseMarkdown(text)) element.appendChild(node);
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
  renderBody(body, message.content, message.role === "user");
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
    if (message.cost_millicents) {
      add("статистика", () => showStats(`/api/stats/message/${message.id}`));
    }
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
  // Разбор разметки на каждый чанк — лишняя работа: за ответ их сотни.
  // Перерисовываем не чаще, чем раз в 120 мс, и обязательно в конце.
  let lastDraw = 0;

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
        const now = Date.now();
        if (now - lastDraw > 120) {
          lastDraw = now;
          renderBody(body, contentText);
          scrollDown(false);
        }
      } else if (event.type === "error") {
        throw new Error(event.message);
      } else if (event.type === "done") {
        renderBody(body, contentText);
        scrollDown(false);
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

  // Второй пустой чат не нужен: если в открытом ещё ничего нет, создавать
  // нечего — иначе от случайных нажатий копятся пустышки.
  const empty = hasChat && !state.messages.length;
  const newChat = $("new-chat");
  newChat.disabled = empty || state.busy;
  newChat.title = empty ? "Этот чат ещё пустой — пиши прямо в него" : "";

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

/* ----------------------------------------------------------- настройки */

function applyTheme(theme) {
  state.theme = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = state.theme;
}

function settingBlock(labelText) {
  const box = document.createElement("div");
  box.className = "setting";
  if (labelText) {
    const label = document.createElement("label");
    label.textContent = labelText;
    box.appendChild(label);
  }
  return box;
}

function note(text) {
  const div = document.createElement("div");
  div.className = "note";
  div.textContent = text;
  return div;
}

function openSettings() {
  const persona = state.persona;
  $("modal-title").textContent = "Настройки";
  const body = $("modal-body");
  body.replaceChildren();

  // --- имя -------------------------------------------------------------
  const nameBox = settingBlock("Имя личности");
  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.value = persona.name;
  nameBox.appendChild(nameInput);
  body.appendChild(nameBox);

  // --- системный промпт -------------------------------------------------
  const promptBox = settingBlock("Системный промпт личности");
  const promptArea = document.createElement("textarea");
  promptArea.value = persona.system_prompt || "";
  promptBox.append(
    promptArea,
    note(
      "Это образец для новых чатов. Уже созданные чаты держат собственную " +
        "копию промпта и отсюда не меняются — промпт отдельного чата " +
        "правится прямо в нём, блоком сверху."
    )
  );
  body.appendChild(promptBox);

  // --- размышление -------------------------------------------------------
  const thinkBox = settingBlock("");
  const switchLabel = document.createElement("label");
  switchLabel.className = "switch";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = Boolean(persona.thinking_enabled);
  const switchText = document.createElement("span");
  switchText.textContent = "Режим размышления";
  switchLabel.append(checkbox, switchText);
  thinkBox.append(
    switchLabel,
    note(
      "Дороже и медленнее: размышления тарифицируются как выходные токены. " +
        "Температура в этом режиме не действует вовсе."
    )
  );
  body.appendChild(thinkBox);

  // --- сохранить ---------------------------------------------------------
  const saveRow = document.createElement("div");
  const save = document.createElement("button");
  save.className = "primary";
  save.style.marginTop = "0";
  save.style.width = "auto";
  save.style.padding = "9px 20px";
  save.textContent = "Сохранить";
  const saved = document.createElement("span");
  saved.className = "saved-note";
  save.onclick = async () => {
    save.disabled = true;
    saved.textContent = "";
    try {
      await api(`/api/personas/${persona.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          name: nameInput.value,
          system_prompt: promptArea.value,
          thinking_enabled: checkbox.checked,
        }),
      });
      const fresh = await api("/api/bootstrap");
      state.personas = fresh.personas;
      await loadPersona();
      saved.textContent = "сохранено";
    } catch (error) {
      fail(error);
    }
    save.disabled = false;
  };
  saveRow.append(save, saved);
  body.appendChild(saveRow);

  // --- тема --------------------------------------------------------------
  const themeBox = settingBlock("Тема");
  themeBox.style.marginTop = "26px";
  const choice = document.createElement("div");
  choice.className = "theme-choice";
  for (const [value, label] of [["dark", "Тёмная"], ["light", "Светлая"]]) {
    const button = document.createElement("button");
    button.textContent = label;
    if (state.theme === value) button.classList.add("current");
    button.onclick = async () => {
      applyTheme(value);
      for (const other of choice.children) other.classList.remove("current");
      button.classList.add("current");
      try {
        await api("/api/settings", {
          method: "PATCH",
          body: JSON.stringify({ theme: value }),
        });
      } catch (error) {
        fail(error);
      }
    };
    choice.appendChild(button);
  }
  themeBox.appendChild(choice);
  body.appendChild(themeBox);

  // --- удаление личности --------------------------------------------------
  const zone = document.createElement("div");
  zone.className = "danger-zone";
  const remove = document.createElement("button");
  remove.textContent = "Удалить личность";
  remove.onclick = () => removePersona(persona);
  zone.append(
    remove,
    note(
      "Сотрёт имя, промпт и все чаты этой личности без возможности " +
        "восстановления. В статистике останется строка без названия, " +
        "чтобы расходы не выпали из общей суммы."
    )
  );
  body.appendChild(zone);

  $("modal").classList.remove("hidden");
}

async function removePersona(persona) {
  const question =
    `Удалить личность «${persona.name}»?\n\n` +
    "Вместе с ней стираются все её чаты и системный промпт.\n" +
    "Восстановить будет нельзя.";
  if (!confirm(question)) return;
  try {
    const result = await api(`/api/personas/${persona.id}`, { method: "DELETE" });
    closeModal();
    if (result.next_persona_id) {
      const fresh = await api("/api/bootstrap");
      state.personas = fresh.personas;
      state.personaId = result.next_persona_id;
      state.chatId = null;
      await loadPersona();
      await loadChats();
    } else {
      location.reload();
    }
  } catch (error) {
    fail(error);
  }
}

/* ---------------------------------------------------------- статистика */

const CATEGORY = {
  cache_hit: "Вход, попал в кэш",
  cache_miss: "Вход, мимо кэша",
  output: "Выход",
};

function closeModal() {
  $("modal").classList.add("hidden");
}

async function showStats(url) {
  try {
    renderStats(await api(url), url.endsWith("/all"));
  } catch (error) {
    fail(error);
  }
}

function sectionTitle(text) {
  const div = document.createElement("div");
  div.className = "stat-section";
  div.textContent = text;
  return div;
}

function cell(text, numeric) {
  const td = document.createElement("td");
  if (numeric) td.className = "num";
  td.textContent = text;
  return td;
}

function statTable(headers) {
  const table = document.createElement("table");
  table.className = "stat";
  const tr = document.createElement("tr");
  for (const [i, h] of headers.entries()) {
    const th = document.createElement("th");
    th.textContent = h;
    if (i) th.style.textAlign = "right";
    tr.appendChild(th);
  }
  table.appendChild(tr);
  return table;
}

function renderStats(data, withBalance) {
  $("modal-title").textContent = data.title;
  const body = $("modal-body");
  body.replaceChildren();

  const total = document.createElement("div");
  total.className = "stat-total";
  total.textContent = formatCost(data.total);
  body.appendChild(total);

  const sub = document.createElement("div");
  sub.className = "stat-sub";
  const bits = [`вызовов к API: ${data.calls}`];
  if (data.context_tokens) {
    bits.push(`контекст ${data.context_tokens.toLocaleString("ru")} токенов`);
  }
  if (data.since) bits.push(`с ${data.since.slice(0, 10)}`);
  sub.textContent = bits.join(" · ");
  body.appendChild(sub);

  if (!data.calls) {
    body.appendChild(sectionTitle("Запросов к API здесь ещё не было"));
    $("modal").classList.remove("hidden");
    return;
  }

  // --- на что ушли деньги -------------------------------------------
  body.appendChild(sectionTitle("На что ушли деньги"));
  const table = statTable(["", "токенов", "цена", "доля"]);
  for (const key of ["cache_hit", "cache_miss", "output"]) {
    const cost = data.cost[key];
    const share = data.total ? (cost / data.total) * 100 : 0;

    const name = document.createElement("td");
    name.appendChild(document.createTextNode(CATEGORY[key]));
    const bar = document.createElement("div");
    bar.className = "bar";
    bar.style.width = Math.max(share, 0.5) + "%";
    name.appendChild(bar);

    const tr = document.createElement("tr");
    tr.append(
      name,
      cell(data.tokens[key].toLocaleString("ru"), true),
      cell(formatCost(cost), true),
      cell(share.toFixed(0) + "%", true)
    );
    table.appendChild(tr);

    if (key === "output" && data.tokens.reasoning) {
      const note = document.createElement("tr");
      note.className = "sub";
      note.append(
        cell("из них размышление"),
        cell(data.tokens.reasoning.toLocaleString("ru"), true),
        cell("", true),
        cell("", true)
      );
      table.appendChild(note);
    }
  }
  body.appendChild(table);

  // --- тариф ----------------------------------------------------------
  const peak = data.by_tariff.peak || 0;
  const off = data.by_tariff.offpeak || 0;
  if (peak || off) {
    body.appendChild(sectionTitle("По тарифу"));
    const t = statTable(["", "цена"]);
    const rowFor = (label, value) => {
      const tr = document.createElement("tr");
      tr.append(cell(label), cell(formatCost(value), true));
      return tr;
    };
    t.append(
      rowFor("Непиковый — вполцены", off),
      rowFor("Пиковый — 01–04 и 06–10 UTC, будни", peak)
    );
    body.appendChild(t);
  }

  // --- разбивка по строкам -------------------------------------------
  if (data.rows && data.rows.length) {
    body.appendChild(sectionTitle(data.rows_header));
    const t = statTable(["", "вызовов", "цена"]);
    for (const row of data.rows) {
      const tr = document.createElement("tr");
      tr.append(
        cell(row.label),
        cell(String(row.calls), true),
        cell(formatCost(row.total), true)
      );
      t.appendChild(tr);
    }
    body.appendChild(t);
  }

  // --- сверка с настоящим балансом ------------------------------------
  if (withBalance) {
    const line = document.createElement("div");
    line.className = "balance-line";
    const text = document.createElement("span");
    text.textContent = "Наш подсчёт можно сверить с настоящим счётом DeepSeek.";
    const button = document.createElement("button");
    button.textContent = "Узнать баланс";
    button.onclick = async () => {
      button.disabled = true;
      button.textContent = "Спрашиваю…";
      try {
        const info = await api("/api/balance");
        const first = (info.balance_infos || [])[0];
        text.textContent = first
          ? `На счету ${first.total_balance} ${first.currency}` +
            (info.is_available ? "" : " — средств не хватает")
          : "DeepSeek не вернул данных о балансе";
        button.remove();
      } catch (error) {
        text.textContent = error.message;
        button.disabled = false;
        button.textContent = "Ещё раз";
      }
    };
    line.append(text, button);
    body.appendChild(line);
  }

  $("modal").classList.remove("hidden");
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
  item("Статистика", () => showStats(`/api/stats/chat/${chat.id}`));

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

  const settings = document.createElement("button");
  settings.textContent = "Настройки";
  settings.onclick = () => {
    closeMenu();
    openSettings();
  };
  menu.appendChild(settings);

  const personaStats = document.createElement("button");
  personaStats.textContent = "Статистика личности";
  personaStats.onclick = () => {
    closeMenu();
    showStats(`/api/stats/persona/${state.personaId}`);
  };
  menu.appendChild(personaStats);

  const allStats = document.createElement("button");
  allStats.textContent = "Статистика за всё время";
  allStats.onclick = () => {
    closeMenu();
    showStats("/api/stats/all");
  };
  menu.appendChild(allStats);

  const separator2 = document.createElement("div");
  separator2.className = "sep";
  menu.appendChild(separator2);

  const create = document.createElement("button");
  create.textContent = "Создать личность";
  create.onclick = () => {
    closeMenu();
    showSetup(true);
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
  if (state.busy || (state.chatId && !state.messages.length)) return;
  try {
    const chat = await api(`/api/personas/${state.personaId}/chats`, { method: "POST" });
    state.chatId = chat.id;
    await loadChats();
    $("input").focus();
  } catch (error) {
    fail(error);
  }
};

$("modal-close").onclick = closeModal;
$("modal").onclick = (event) => {
  if (event.target.id === "modal") closeModal();
};
$("setup-cancel").onclick = hideSetup;

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeModal();
    closeMenu();
    hideSetup();
  }
});

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
