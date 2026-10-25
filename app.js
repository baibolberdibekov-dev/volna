'use strict';

const $ = (selector) => document.querySelector(selector);
const state = { key: '', job: null, selected: null, pollId: 0, autoDownload: false, config: null };
const storage = {
  get(key, fallback = null) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Storage is optional. */ } },
};
try { state.key = sessionStorage.getItem('volna-key') || ''; } catch { /* Private browsing. */ }

function notice(message = '') {
  $('#notice').textContent = message;
  $('#notice').hidden = !message;
}

async function api(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 75000);
  try {
    const response = await fetch(path, {
      ...options,
      headers: { 'Content-Type': 'application/json', 'X-App-Key': state.key, ...options.headers },
      signal: controller.signal,
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      if (response.status === 401) { $('#key-form').hidden = false; $('#app-key').focus(); }
      const message = typeof data?.detail === 'string' ? data.detail : response.status === 422
        ? 'Проверь название песни: нужно от 2 до 160 символов.'
        : 'Сервер пока не ответил. Подожди немного и попробуй снова.';
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    if (!data) throw new Error('Сервер запускается или недоступен. Попробуй ещё раз через минуту.');
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('Ответ занял слишком много времени. Попробуй ещё раз.');
    if (error instanceof TypeError) throw new Error('Не удалось связаться с сервером. Проверь подключение к интернету.');
    throw error;
  } finally { clearTimeout(timeout); }
}

function duration(seconds) {
  if (!seconds) return '—';
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function renderRecent() {
  const items = storage.get('volna-recent', []);
  const list = Array.isArray(items) ? items.filter(x => typeof x === 'string').slice(0, 4) : [];
  $('#recent').hidden = !list.length;
  $('#recent-items').replaceChildren();
  list.forEach(query => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = query;
    button.addEventListener('click', () => search(query));
    $('#recent-items').append(button);
  });
}

function saveQuery(query) {
  const old = storage.get('volna-recent', []);
  const safe = Array.isArray(old) ? old.filter(x => typeof x === 'string') : [];
  storage.set('volna-recent', [query, ...safe.filter(x => x.toLowerCase() !== query.toLowerCase())].slice(0, 4));
  renderRecent();
}

function busyDownloads() {
  const busy = !!state.job && !['ready', 'error'].includes(state.job.status);
  document.querySelectorAll('.track-download').forEach(button => { button.disabled = busy; });
}

function emptyResults(title, description, isError = false) {
  const box = document.createElement('div');
  box.className = `empty-state${isError ? ' error-state' : ''}`;
  const heading = document.createElement('h3');
  heading.textContent = title;
  const text = document.createElement('p');
  text.textContent = description;
  box.append(heading, text);
  $('#results').replaceChildren(box);
}

function renderResults(tracks) {
  $('#results').replaceChildren();
  $('#result-count').textContent = `${tracks.length} РЕЗУЛЬТАТОВ`;
  $('#search-status').textContent = `Найдено треков: ${tracks.length}.`;
  if (!tracks.length) {
    emptyResults('Пока ничего не нашлось', 'Попробуй добавить исполнителя или изменить название. Длинные записи и прямые эфиры скрыты.');
    return;
  }
  tracks.forEach((track, index) => {
    const row = $('#track-template').content.firstElementChild.cloneNode(true);
    row.querySelector('.track-number').textContent = String(index + 1).padStart(2, '0');
    row.querySelector('.track-cover').src = track.thumbnail;
    row.querySelector('h3').textContent = track.title;
    row.querySelector('.track-info p').textContent = track.artist;
    row.querySelector('.track-duration').textContent = duration(track.duration);
    row.querySelector('.youtube-link').href = track.url;
    const button = row.querySelector('.track-download');
    button.setAttribute('aria-label', `Подготовить MP3: ${track.title}`);
    button.addEventListener('click', () => startDownload(track));
    $('#results').append(row);
  });
  busyDownloads();
}

let searchSequence = 0;
async function search(value) {
  const query = value.trim().replace(/\s+/g, ' ');
  if (query.length < 2) { notice('Введи хотя бы два символа.'); $('#search-input').focus(); return; }
  const sequence = ++searchSequence;
  $('#search-input').value = query;
  notice();
  $('#search-button').disabled = true;
  $('#search-button span:first-child').textContent = 'Ищем…';
  $('#results-title').textContent = 'Ищем твою музыку';
  $('#result-count').textContent = 'YOUTUBE';
  $('#search-status').textContent = 'Идёт поиск на YouTube.';
  $('#results').setAttribute('aria-busy', 'true');
  const copy = document.createElement('p');
  copy.className = 'searching-copy';
  copy.textContent = 'Обычно поиск занимает несколько секунд. Первый запрос может быть дольше.';
  $('#results').replaceChildren(copy);
  for (let i = 0; i < 3; i++) {
    const skeleton = document.createElement('div');
    skeleton.className = 'skeleton';
    skeleton.setAttribute('aria-hidden', 'true');
    $('#results').append(skeleton);
  }
  try {
    const data = await api('/api/search', { method: 'POST', body: JSON.stringify({ query }) });
    if (sequence !== searchSequence) return;
    $('#results-title').textContent = 'Результаты поиска';
    renderResults(data.tracks);
    saveQuery(query);
  } catch (error) {
    if (sequence !== searchSequence) return;
    $('#results-title').textContent = 'Поиск не завершён';
    $('#result-count').textContent = 'ПОПРОБУЙ ЕЩЁ РАЗ';
    $('#search-status').textContent = error.message;
    emptyResults('Не удалось выполнить поиск', error.message, true);
  } finally {
    if (sequence === searchSequence) {
      $('#search-button').disabled = false;
      $('#search-button span:first-child').textContent = 'Найти трек';
      $('#results').setAttribute('aria-busy', 'false');
    }
  }
}

const statusLabels = {
  queued: 'В очереди', checking: 'Проверяем трек', downloading: 'Загружаем аудио',
  converting: 'Создаём MP3', ready: 'Готово к скачиванию', error: 'Не получилось',
};

function rememberJob() {
  try { sessionStorage.setItem('volna-job', JSON.stringify({ id: state.job.id, track: state.selected })); } catch { /* Optional. */ }
}

function showJob(job) {
  state.job = job;
  const track = job.track || state.selected;
  if (track) {
    state.selected = track;
    $('#selected-title').textContent = track.title;
    $('#selected-artist').textContent = track.artist;
    $('#selected-cover').src = track.thumbnail;
  }
  $('#download-empty').hidden = true;
  $('#download-content').hidden = false;
  $('#job-label').textContent = statusLabels[job.status] || 'Подготавливаем';
  $('#job-percent').textContent = job.status === 'queued' ? '…' : `${job.progress || 0}%`;
  $('#job-progress').value = job.progress || 0;
  $('#download-link').hidden = job.status !== 'ready';
  $('#audio-player').hidden = job.status !== 'ready';
  $('#retry-download').hidden = job.status !== 'error';
  $('#retry-download').textContent = 'Повторить';
  if (job.status === 'ready') {
    $('#download-link').href = job.download_url;
    $('#download-link').download = `${track?.title || 'track'}.mp3`;
    const player = $('#audio-player');
    if (player.getAttribute('src') !== `${job.download_url}&preview=true`) player.src = `${job.download_url}&preview=true`;
    $('#job-detail').textContent = `${(job.size / (1024 * 1024)).toFixed(1)} МБ · MP3 · ${job.quality} кбит/с`;
    if (state.autoDownload) {
      state.autoDownload = false;
      $('#download-link').click();
    }
  } else if (job.status === 'error') {
    $('#job-detail').textContent = job.error;
  } else {
    $('#job-detail').textContent = job.status === 'queued'
      ? 'Скоро начнём. Можно продолжать искать музыку.'
      : 'Файл скачается после подготовки. Кнопка скачивания тоже останется здесь.';
  }
  busyDownloads();
}

async function startDownload(track) {
  if (state.job && !['ready', 'error'].includes(state.job.status)) return;
  notice();
  state.pollId++;
  state.selected = track;
  state.autoDownload = true;
  const quality = Number(document.querySelector('input[name="quality"]:checked').value);
  $('#audio-player').pause();
  $('#audio-player').removeAttribute('src');
  $('#audio-player').load();
  showJob({ status: 'queued', progress: 0, track, quality });
  if (matchMedia('(max-width: 760px)').matches) $('.download-panel').scrollIntoView({ behavior: 'smooth', block: 'center' });
  try {
    const job = await api('/api/downloads', { method: 'POST', body: JSON.stringify({ video_id: track.id, quality }) });
    showJob(job);
    rememberJob();
    if (!['ready', 'error'].includes(job.status)) pollJob(job.id);
  } catch (error) {
    state.autoDownload = false;
    showJob({ status: 'error', progress: 0, error: error.message, track, quality });
  }
}

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function pollJob(id) {
  const generation = ++state.pollId;
  let failures = 0;
  while (generation === state.pollId) {
    await delay(failures ? 5000 : 1500);
    if (generation !== state.pollId) return;
    try {
      const job = await api(`/api/downloads/${encodeURIComponent(id)}`);
      if (generation !== state.pollId) return;
      failures = 0;
      showJob(job);
      if (['ready', 'error'].includes(job.status)) return;
    } catch (error) {
      if (generation !== state.pollId) return;
      if (error.status === 404) {
        state.autoDownload = false;
        showJob({ ...state.job, status: 'error', error: error.message });
        return;
      }
      failures++;
      $('#job-detail').textContent = `${error.message} Переподключаемся…`;
      if (error.status === 401 || failures >= 6) {
        $('#job-detail').textContent = 'Не удалось получить статус. Подготовка на сервере может продолжаться.';
        $('#retry-download').textContent = 'Проверить статус';
        $('#retry-download').hidden = false;
        return;
      }
    }
  }
}

$('#search-form').addEventListener('submit', event => { event.preventDefault(); search($('#search-input').value); });
document.querySelectorAll('[data-query]').forEach(button => button.addEventListener('click', () => search(button.dataset.query)));
$('#clear-recent').addEventListener('click', () => { storage.set('volna-recent', []); renderRecent(); });
$('#retry-download').addEventListener('click', () => {
  if (state.job?.id && state.job.status !== 'error') { $('#retry-download').hidden = true; pollJob(state.job.id); }
  else if (state.selected) startDownload(state.selected);
});
$('#key-form').addEventListener('submit', event => {
  event.preventDefault();
  state.key = $('#app-key').value.trim();
  try { sessionStorage.setItem('volna-key', state.key); } catch { /* Optional. */ }
  $('#key-status').textContent = 'Ключ сохранён для этой вкладки.';
  $('#search-input').focus();
});
$('#audio-player').addEventListener('error', () => {
  if ($('#audio-player').getAttribute('src')) $('#job-detail').textContent = 'Файл мог истечь или сервер перезапустился. Подготовь этот трек ещё раз.';
});

async function initialize() {
  renderRecent();
  $('#app-key').value = state.key;
  try {
    state.config = await api('/api/config');
    $('#key-form').hidden = !state.config.requires_key;
    $('#ttl-note').textContent = `Скачай свой трек в течение ${state.config.file_ttl_minutes} мин.`;
    $('#limit-note').textContent = `Треки до ${state.config.max_duration_minutes} минут.`;
    const saved = (() => { try { return JSON.parse(sessionStorage.getItem('volna-job')); } catch { return null; } })();
    if (saved?.id && /^[a-f0-9]{32}$/.test(saved.id)) {
      state.selected = saved.track;
      showJob({ id: saved.id, status: 'queued', progress: 0, track: saved.track });
      pollJob(saved.id);
    }
  } catch (error) {
    notice(`Не удалось подключиться к серверу. ${error.message}`);
  }
}
initialize();
