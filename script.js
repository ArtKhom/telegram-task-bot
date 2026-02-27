// Без жодних ключів Supabase — всі запити йдуть через бекенд
const API_URL = 'https://worthy-contentment-production.up.railway.app/api/create-department-task';

let tg = null;
try { tg = window.Telegram?.WebApp ?? null; if (tg) tg.expand(); } catch(e) {}

let currentDepartment = null;

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
  currentDepartment = null;
}

async function submitTask() {
  const input = document.getElementById('modal-input');
  const taskTitle = input.value.trim();

  if (!taskTitle) {
    input.classList.add('error');
    input.focus();
    return;
  }

  try {
    const response = await fetch(API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: taskTitle,
        department: currentDepartment,
        author: tg?.initDataUnsafe?.user?.first_name ?? 'Admin'
      })
    });

    const result = await response.json();

    if (result.error) {
      alert('Ошибка: ' + result.error);
    } else {
      closeModal();
      alert('✅ Задача создана в отдел ' + currentDepartment);
    }
  } catch(e) {
    alert('Ошибка: ' + e.message);
  }
}
