const tasksMain = document.getElementById("tasks-main");
const tasksIntro = document.getElementById("tasks-intro");
const tasksChecklist = document.getElementById("tasks-checklist");
const tasksList = document.getElementById("tasks-list");
const tasksStatus = document.getElementById("tasks-status");
const getTasksButton = document.getElementById("get-tasks");
const retryTasksButton = document.getElementById("retry-tasks");
const taskTemplate = document.getElementById("task-template");
let taskState = null;
let tasksBusy = false;
let retryAction = loadTasks;

function setTasksBusy(busy) {
  tasksBusy = busy;
  tasksMain.setAttribute("aria-busy", String(busy));
  getTasksButton.disabled = busy || !taskState;
  retryTasksButton.disabled = busy;
  tasksList.querySelectorAll("button").forEach((control) => {
    control.disabled = busy;
  });
}

function showTasksStatus(message, isError = false) {
  tasksStatus.textContent = message;
  tasksStatus.closest(".tasks-feedback").hidden = !message;
  tasksStatus.classList.toggle("is-error", isError);
  retryTasksButton.hidden = true;
}

function showTasksError(message, action) {
  showTasksStatus(message, true);
  retryAction = action;
  retryTasksButton.hidden = false;
}

async function requestTasks(url, method = "GET", payload) {
  const options = { method, credentials: "same-origin", cache: "no-store" };
  if (payload !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || "Не удалось загрузить задания.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function createTaskItem(task) {
  const item = taskTemplate.content.firstElementChild.cloneNode(true);
  const text = item.querySelector(".task-text");
  const button = item.querySelector("button");
  item.dataset.taskId = task.id;
  item.classList.toggle("is-completed", task.completed);
  item.classList.toggle("is-optional", task.optional);
  text.id = `task-text-${task.id}`;
  text.querySelector(".task-optional").hidden = !task.optional;
  text.querySelector(".task-description").textContent = task.text;
  button.textContent = task.completed ? "Отменить" : "Выполнил";
  button.setAttribute("aria-describedby", text.id);
  return item;
}

function renderTasks(state) {
  taskState = state;
  tasksIntro.hidden = state.assigned;
  tasksChecklist.hidden = !state.assigned;
  tasksList.replaceChildren(...state.tasks.map(createTaskItem));
}

async function loadTasks() {
  if (tasksBusy) return;
  setTasksBusy(true);
  showTasksStatus("");
  try {
    const state = await requestTasks("/api/tasks");
    renderTasks(state);
  } catch {
    showTasksError(
      "Не удалось загрузить задания. Попробуй ещё раз.",
      loadTasks,
    );
  } finally {
    setTasksBusy(false);
  }
}

async function assignTasks() {
  if (tasksBusy) return;
  setTasksBusy(true);
  getTasksButton.textContent = "Получаем задания…";
  showTasksStatus("");
  try {
    renderTasks(await requestTasks("/api/tasks", "POST", {}));
    tasksList.querySelector("button")?.focus({ preventScroll: true });
  } catch (error) {
    if (error.status === 409) showTasksStatus(error.message);
    else
      showTasksError(
        "Не удалось получить задания. Попробуй ещё раз.",
        assignTasks,
      );
  } finally {
    getTasksButton.textContent = "Получить задания";
    setTasksBusy(false);
  }
}

async function updateTask(id, completed) {
  if (tasksBusy) return;
  setTasksBusy(true);
  showTasksStatus("");
  try {
    renderTasks(await requestTasks(`/api/tasks/${id}`, "PATCH", { completed }));
    tasksList
      .querySelector(`[data-task-id="${id}"] button`)
      ?.focus({ preventScroll: true });
  } catch {
    showTasksError("Не удалось сохранить отметку. Попробуй ещё раз.", () =>
      updateTask(id, completed),
    );
  } finally {
    setTasksBusy(false);
  }
}

function handleTaskClick(event) {
  const button = event.target.closest("[data-task-action]");
  if (!button) return;
  const id = Number(button.closest("[data-task-id]").dataset.taskId);
  const task = taskState.tasks.find((entry) => entry.id === id);
  updateTask(id, !task.completed);
}

getTasksButton.addEventListener("click", assignTasks);
retryTasksButton.addEventListener("click", () => retryAction());
tasksList.addEventListener("click", handleTaskClick);
loadTasks();
