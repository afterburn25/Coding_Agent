/* Nexus Core · Mission Control */

const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<">']/g, c => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;'
}[c]);

async function api(path, method = 'GET', body) {
  const res = await fetch(`/api${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined
  });
  return await res.json();
}

async function loadMissionReport(id) {
  try {
    const report = await api(`/missions/${id}/report`);
    const reportDiv = document.createElement('div');
    reportDiv.className = 'detail-section';

    const title = document.createElement('h3');
    title.textContent = 'Report';
    reportDiv.appendChild(title);

    const summary = document.createElement('div');
    summary.className = 'objective';
    summary.textContent = `Title: ${report.title}
Status: ${report.status}
Metrics: ${report.metrics}`;
    reportDiv.appendChild(summary);

    const workstreams = document.createElement('div');
    workstreams.className = 'objective';
    workstreams.textContent = 'Workstreams:
' + report.workstreams.map(ws => `${ws.name}: ${ws.progress}`).join('\n');
    reportDiv.appendChild(workstreams);

    document.querySelector('.mission-detail').appendChild(reportDiv);
  } catch (err) {
    console.error('Failed to load mission report:', err);
  }
}

// Load mission report when the mission detail page is ready
window.addEventListener('DOMContentLoaded', () => {
  const missionId = window.location.pathname.split('/').pop();
  if (missionId) {
    loadMissionReport(missionId);
  }
});