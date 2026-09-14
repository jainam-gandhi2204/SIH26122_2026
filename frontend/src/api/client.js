/**
 * API client module for SIH26122 backend communication.
 * Communicates directly with FastAPI backend via Vite proxy.
 */

const BASE_URL = '';

async function request(url, options = {}) {
  const config = {
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
    ...options,
  };

  // Do not set Content-Type header if sending FormData (browser sets boundary)
  if (options.body instanceof FormData) {
    delete config.headers['Content-Type'];
  }

  const response = await fetch(`${BASE_URL}${url}`, config);

  if (!response.ok) {
    let errorDetail = response.statusText;
    try {
      const errJson = await response.json();
      errorDetail = errJson.detail || JSON.stringify(errJson);
    } catch {
      // ignore parse errors
    }
    const error = new Error(typeof errorDetail === 'string' ? errorDetail : JSON.stringify(errorDetail));
    error.status = response.status;
    throw error;
  }

  return response.json();
}

// ---------------------------------------------------------------------------
// Schedule & Deviation APIs
// ---------------------------------------------------------------------------

export async function getScheduleDeviation() {
  return request('/schedule/deviation');
}

export async function getLinkedTasks() {
  return request('/schedule/tasks/linked');
}

export async function getTask(taskId) {
  return request(`/schedule/tasks/${taskId}`);
}

export async function getTaskImpact(taskId) {
  return request(`/schedule/tasks/${taskId}/impact`);
}

export const fetchTaskImpact = getTaskImpact;

export async function importSchedule(file, replace = false) {
  const formData = new FormData();
  formData.append('file', file);
  const query = replace ? '?replace=true' : '';
  return request(`/schedule/import${query}`, {
    method: 'POST',
    body: formData,
  });
}

export const importScheduleSpreadsheet = importSchedule;

// ---------------------------------------------------------------------------
// Site Updates & Spreadsheet Ingestion APIs
// ---------------------------------------------------------------------------

export async function getSiteUpdates(status = 'active') {
  const query = status ? `?status=${encodeURIComponent(status)}` : '';
  return request(`/site-updates${query}`);
}

export async function getSiteUpdateResult(updateId) {
  return request(`/site-updates/${updateId}/process`);
}

export async function createSiteUpdate(payload) {
  return request('/site-updates', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function processSiteUpdate(updateId) {
  return request(`/site-updates/${updateId}/process`, {
    method: 'POST',
  });
}

export async function ingestSiteUpdate({ text, raw_update, reported_date, reported_on, location, source_update_id }) {
  let formattedDate = '09-Sep-2026';
  const rawDate = reported_date || reported_on;
  if (rawDate) {
    if (/^\d{2}-[A-Za-z]{3}-\d{4}$/.test(rawDate)) {
      formattedDate = rawDate;
    } else if (/^\d{4}-\d{2}-\d{2}$/.test(rawDate)) {
      const [year, month, day] = rawDate.split('-');
      const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
      const monthIdx = parseInt(month, 10) - 1;
      formattedDate = `${day.padStart(2, '0')}-${months[monthIdx]}-${year}`;
    } else {
      const d = new Date(rawDate);
      if (!isNaN(d.getTime())) {
        const day = String(d.getDate()).padStart(2, '0');
        const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
        formattedDate = `${day}-${months[d.getMonth()]}-${d.getFullYear()}`;
      }
    }
  }

  const generatedSourceId = source_update_id || `UPD-${Date.now().toString().slice(-6)}`;

  let resolvedLocation = (location || '').trim();
  if (!resolvedLocation) {
    const rawText = text || raw_update || '';
    const locMatch = rawText.match(/\bat\s+([A-Za-z0-9\s]+?)(?:\s+is|\s+was|\s*\.|\s*,|$)/i);
    if (locMatch && locMatch[1] && locMatch[1].trim().length < 30) {
      resolvedLocation = locMatch[1].trim();
    } else {
      resolvedLocation = 'Well Pad A';
    }
  }

  const updatePayload = {
    source_update_id: generatedSourceId,
    raw_update: text || raw_update,
    location: resolvedLocation,
    reported_on: formattedDate,
    source_reference: 'Manual Ingestion',
  };

  const created = await createSiteUpdate(updatePayload);
  const targetUpdateId = created?.source_update_id || generatedSourceId;
  if (targetUpdateId) {
    try {
      const processed = await processSiteUpdate(targetUpdateId);
      return { created, ...processed };
    } catch (err) {
      console.warn('Auto AI processing on new update failed:', err);
    }
  }
  return created;
}

export async function importSpreadsheet(file) {
  const formData = new FormData();
  formData.append('file', file);
  const result = await request('/site-updates/import-spreadsheet', {
    method: 'POST',
    body: formData,
  });

  // Automatically trigger processing on accepted records so they link to schedule
  if (result && Array.isArray(result.records) && result.records.length > 0) {
    const processPromises = result.records.map(async (r) => {
      try {
        const updateId = r.source_update_id || r.id;
        return await processSiteUpdate(updateId);
      } catch (err) {
        console.warn('Auto-processing imported record failed:', r, err);
        return null;
      }
    });
    const processedResults = await Promise.all(processPromises);
    result.processed = processedResults.filter(Boolean);
  }

  return result;
}

export const uploadSiteUpdatesSpreadsheet = importSpreadsheet;

// ---------------------------------------------------------------------------
// Planner Review Queue APIs
// ---------------------------------------------------------------------------

export async function getReviewQueue(status = 'pending', threshold = 70.0) {
  return request(`/planner/review-queue?status=${encodeURIComponent(status)}&threshold=${threshold}`);
}

export async function approveReviewItem(reviewId, notes = null) {
  return request(`/planner/review-queue/${reviewId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ notes }),
  });
}

export async function changeReviewItemMatch(reviewId, taskId, notes = null) {
  return request(`/planner/review-queue/${reviewId}/change-match`, {
    method: 'POST',
    body: JSON.stringify({ task_id: taskId, notes }),
  });
}

export async function rejectReviewItem(reviewId, reason = null) {
  return request(`/planner/review-queue/${reviewId}/reject`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  });
}

export async function resolveReviewItem(reviewId, action, taskId = null, notes = null) {
  return request(`/planner/review-queue/${reviewId}/resolve`, {
    method: 'POST',
    body: JSON.stringify({ action, task_id: taskId, notes }),
  });
}

// ---------------------------------------------------------------------------
// System Health
// ---------------------------------------------------------------------------

export async function checkHealth() {
  return request('/health');
}

