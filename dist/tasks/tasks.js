const tasksMain = document.getElementById("tasks-main");
const tasksIntro = document.getElementById("tasks-intro");
const tasksChecklist = document.getElementById("tasks-checklist");
const tasksList = document.getElementById("tasks-list");
const tasksProgress = document.getElementById("tasks-progress");
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
  tasksList.querySelectorAll("input, button").forEach((control) => {
    control.disabled = busy;
  });
}

function showTasksStatus(message, isError = false) {
  tasksStatus.textContent = message;
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
  const checkbox = item.querySelector("input");
  const label = item.querySelector("label");
  const button = item.querySelector("button");
  const kind = item.querySelector(".task-kind");
  item.dataset.taskId = task.id;
  item.classList.toggle("is-completed", task.completed);
  item.classList.toggle("is-optional", task.optional);
  kind.id = `task-kind-${task.id}`;
  kind.textContent = task.optional
    ? "Необязательно · С нейронкой"
    : "Обязательное";
  checkbox.setAttribute("aria-describedby", kind.id);
  checkbox.id = `task-${task.id}`;
  checkbox.checked = task.completed;
  label.htmlFor = checkbox.id;
  label.id = `task-text-${task.id}`;
  label.textContent = task.text;
  button.textContent = task.completed ? "Отменить" : "Выполнил";
  button.setAttribute("aria-describedby", label.id);
  return item;
}

function renderTasks(state) {
  taskState = state;
  tasksIntro.hidden = state.assigned;
  tasksChecklist.hidden = !state.assigned;
  tasksList.replaceChildren(...state.tasks.map(createTaskItem));
  tasksProgress.textContent = taskProgress(state.tasks);
}

function taskProgress(tasks) {
  const required = tasks.filter((task) => !task.optional);
  const optional = tasks.filter((task) => task.optional);
  const completed = required.filter((task) => task.completed).length;
  const optionalCompleted = optional.filter((task) => task.completed).length;
  const progress =
    completed === required.length && completed > 0
      ? "Все обязательные задания выполнены!"
      : `Обязательные: ${completed} из ${required.length}`;
  return optional.length
    ? `${progress} · С нейронкой: ${optionalCompleted} из ${optional.length}`
    : progress;
}

async function loadTasks() {
  if (tasksBusy) return;
  setTasksBusy(true);
  showTasksStatus("Загружаем задания…");
  try {
    const state = await requestTasks("/api/tasks");
    renderTasks(state);
    showTasksStatus(
      !state.assigned && !state.available
        ? "Задания скоро появятся. Загляни чуть позже."
        : "",
    );
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
    showTasksStatus("Задания готовы!");
    document.getElementById("checklist-title").focus({ preventScroll: true });
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

async function updateTask(id, completed, control = "button") {
  if (tasksBusy) return;
  setTasksBusy(true);
  showTasksStatus("Сохраняем отметку…");
  try {
    renderTasks(await requestTasks(`/api/tasks/${id}`, "PATCH", { completed }));
    tasksList
      .querySelector(`[data-task-id="${id}"] ${control}`)
      ?.focus({ preventScroll: true });
    showTasksStatus(
      completed ? "Сохранено. Задание выполнено." : "Сохранено. Отметка снята.",
    );
  } catch {
    showTasksError("Не удалось сохранить отметку. Попробуй ещё раз.", () =>
      updateTask(id, completed, control),
    );
  } finally {
    setTasksBusy(false);
  }
}

function handleTaskChange(event) {
  const checkbox = event.target.closest(".task-checkbox");
  if (!checkbox) return;
  const id = Number(checkbox.closest("[data-task-id]").dataset.taskId);
  const task = taskState.tasks.find((entry) => entry.id === id);
  const completed = checkbox.checked;
  checkbox.checked = task.completed;
  updateTask(id, completed, "input");
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
tasksList.addEventListener("change", handleTaskChange);
tasksList.addEventListener("click", handleTaskClick);
loadTasks();
