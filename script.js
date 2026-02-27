const API_BASE = 'https://worthy-contentment-production.up.railway.app';

let tg = null;
try { tg = window.Telegram?.WebApp ?? null; if (tg) tg.expand(); } catch(e) {}

const currentUser = tg?.initDataUnsafe?.user?.first_name ?? 'Admin';
let currentDepartment = null;
let taskToDeleteId = null;
let currentView = 'departments'; // 'departments' | 'tasks'

// --- Навігація ---
function showDepartments() {
  currentView = 'departments';
  document.getElementById('screen-departments').style.display = 'block';
  document.getElementById('screen-tasks').style.display = 'none';
}

function showTasks(department) {
  currentView = 'tasks';
  currentDepartment = department;
  document.getElementById('tasks-title').textContent = department ? `Задачи: ${department}` : 'Все задачи';
  document.getElementById('screen-departments').style.display = 'none';
  document.getElementById('screen-tasks').style.display = 'block';
  loadTasks(department);
}

// --- Завантаження задач ---
async function loadTasks(department) {
  const list = document.getElementById('tasks-list');
  list.innerHTML = '<div class="loading">Загрузка...</div>';
  try {
    const url = department
      ? `${API_BASE}/api/department-tasks?department=${encodeURIComponent(department)}`
      : `${API_BASE}/api/department-tasks`;
    const resp = await fetch(url);
    const result = await resp.json();
    renderTasks(result.tasks || []);
  } catch(e) {
    list.innerHTML = '<div class="loading">Ошибка загрузки</div>';
    alert('Ошибка: ' + e.message);
  }
}

function renderTasks(tasks) {
  const list = document.getElementById('tasks-list');
  if (!tasks.length) {
    list.innerHTML = '<div class="loading">Задач пока нет</div>';
    return;
  }
  list.innerHTML = tasks.map(t => `
    <div class="task-card ${t.status === 'done' ? 'task-done' : ''}">
      <div class="task-header">
        <span class="task-dept">${t.department}</span>
        <span class="task-status">${t.status === 'done' ? '✅ Выполнено' : '🕐 Активна'}</span>
      </div>
      <div class="task-title">${t.title}</div>
      <div class="task-meta">
        <span>Создал: <b>${t.author}</b></span>
        ${t.last_modified_by ? `<span>Выполнил: <b>${t.last_modified_by}</b></span>` : ''}
      </div>
      <div class="task-actions">
        ${t.status !== 'done' ? `<button class="btn-complete" onclick="confirmComplete(${t.id})">✅ Выполнить</button>` : ''}
        <button class="btn-delete" onclick="confirmDelete(${t.id}, '${t.title.replace(/'/g, "\\'")}')">🗑 Удалить</button>
      </div>
    </div>
  `).join('');
}

// --- Модальне вікно створення ---
function openModal(departmentName) {
  currentDepartment = departmentName;
  document.getElementById('modal-title').textContent = 'Новая задача: ' + departmentName;
  document.getElementById('modal-input').value = '';
  document.getElementById('modal-input').classList.remove('error');
  document.getElementById('modal-overlay').classList.add('active');
  setTimeout(() => document.getElementById('modal-input').focus(), 150);
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('active');
}

async function submitTask() {
  const input = document.getElementById('modal-input');
  const taskTitle = input.value.trim();
  if (!taskTitle) { input.classList.add('error'); input.focus(); return; }

  try {
    const resp = await fetch(`${API_BASE}/api/create-department-task`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: taskTitle, department: currentDepartment, author: currentUser })
    });
    const result = await resp.json();
    if (result.error) { alert('Ошибка: ' + result.error); }
    else { closeModal(); alert('✅ Задача создана в отдел ' + currentDepartment); }
  } catch(e) { alert('Ошибка: ' + e.message); }
}

// --- Виконання задачі ---
function confirmComplete(taskId) {
  document.getElementById('confirm-overlay').classList.add('active');
  document.getElementById('confirm-text').textContent = 'Отметить задачу как выполненную?';
  document.getElementById('confirm-ok').onclick = () => completeTask(taskId);
}

async function completeTask(taskId) {
  closeConfirm();
  try {
    const resp = await fetch(`${API_BASE}/api/department-tasks/${taskId}/complete`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ modified_by: currentUser })
    });
    const result = await resp.json();
    if (result.error) { alert('Ошибка: ' + result.error); }
    else { loadTasks(currentDepartment); }
  } catch(e) { alert('Ошибка: ' + e.message); }
}

// --- Видалення задачі ---
function confirmDelete(taskId, taskTitle) {
  taskToDeleteId = taskId;
  document.getElementById('confirm-overlay').classList.add('active');
  document.getElementById('confirm-text').textContent = `Вы уверены, что хотите удалить задачу "${taskTitle}"?`;
  document.getElementById('confirm-ok').onclick = () => deleteTask(taskId);
}

async function deleteTask(taskId) {
  closeConfirm();
  try {
    const resp = await fetch(`${API_BASE}/api/department-tasks/${taskId}`, { method: 'DELETE' });
    const result = await resp.json();
    if (result.error) { alert('Ошибка: ' + result.error); }
    else { loadTasks(currentDepartment); }
  } catch(e) { alert('Ошибка: ' + e.message); }
}

function closeConfirm() {
  document.getElementById('confirm-overlay').classList.remove('active');
  taskToDeleteId = null;
}
