/**
 * Formatting and styling helper utilities matching Stitch design conventions.
 */

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function formatDate(dateVal) {
  if (!dateVal) return '—';
  try {
    const d = new Date(dateVal);
    if (isNaN(d.getTime())) return String(dateVal);
    const day = String(d.getDate()).padStart(2, '0');
    const month = MONTH_NAMES[d.getMonth()];
    const year = d.getFullYear();
    return `${day}-${month}-${year}`;
  } catch {
    return String(dateVal);
  }
}

export function formatDateShort(dateVal) {
  if (!dateVal) return '—';
  try {
    const d = new Date(dateVal);
    if (isNaN(d.getTime())) return String(dateVal);
    const day = String(d.getDate()).padStart(2, '0');
    const month = MONTH_NAMES[d.getMonth()];
    return `${month} ${day}`;
  } catch {
    return String(dateVal);
  }
}

export function formatDateTime(dateVal) {
  if (!dateVal) return 'Live';
  try {
    const d = new Date(dateVal);
    if (isNaN(d.getTime())) return String(dateVal);
    const day = String(d.getDate()).padStart(2, '0');
    const month = MONTH_NAMES[d.getMonth()];
    const year = d.getFullYear();
    const timeStr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return `${day} ${month} ${year}, ${timeStr}`;
  } catch {
    return String(dateVal);
  }
}

export function formatDateRange(startVal, endVal) {
  if (!startVal && !endVal) return '—';
  const s = formatDate(startVal);
  const e = formatDate(endVal);
  return `${s} – ${e}`;
}

export function formatTime(dateVal) {
  if (!dateVal) return '';
  try {
    const d = new Date(dateVal);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return '';
  }
}

export function formatRelativeDate(dateVal) {
  if (!dateVal) return '—';
  try {
    const d = new Date(dateVal);
    if (isNaN(d.getTime())) return String(dateVal);
    const now = new Date();
    const isToday =
      d.getDate() === now.getDate() &&
      d.getMonth() === now.getMonth() &&
      d.getFullYear() === now.getFullYear();

    if (isToday) return 'Today';

    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    const isYesterday =
      d.getDate() === yesterday.getDate() &&
      d.getMonth() === yesterday.getMonth() &&
      d.getFullYear() === yesterday.getFullYear();

    if (isYesterday) return 'Yesterday';

    const day = String(d.getDate()).padStart(2, '0');
    const month = MONTH_NAMES[d.getMonth()];
    return `${day}-${month}`;
  } catch {
    return String(dateVal);
  }
}

export function formatRelativeTime(dateVal) {
  if (!dateVal) return 'Recently';
  try {
    const d = new Date(dateVal);
    if (isNaN(d.getTime())) return String(dateVal);
    const diffMs = Date.now() - d.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffMins < 5) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays === 1) return '1d ago';
    return formatDate(dateVal);
  } catch {
    return 'Recently';
  }
}

/**
 * Maps task status / health to Stitch badge colors.
 */
export function getStatusBadgeConfig(status) {
  const clean = (status || '').toLowerCase().trim();
  if (clean.includes('complete')) {
    return {
      label: 'Completed',
      bg: 'bg-emerald-100',
      text: 'text-emerald-800',
      dot: 'bg-teal-500',
      border: 'border-emerald-500',
      progressBg: 'bg-teal-500',
      textColor: 'text-teal-600',
    };
  }
  if (clean.includes('delay')) {
    return {
      label: 'Delayed',
      bg: 'bg-rose-100',
      text: 'text-rose-700',
      dot: 'bg-red-500',
      border: 'border-red-500',
      progressBg: 'bg-red-500',
      textColor: 'text-red-600',
    };
  }
  if (clean.includes('risk')) {
    return {
      label: 'At Risk',
      bg: 'bg-amber-100',
      text: 'text-amber-700',
      dot: 'bg-amber-500',
      border: 'border-amber-500',
      progressBg: 'bg-blue-600',
      textColor: 'text-amber-600',
    };
  }
  if (clean.includes('progress')) {
    return {
      label: 'In Progress',
      bg: 'bg-blue-100',
      text: 'text-blue-700',
      dot: 'bg-blue-600',
      border: 'border-blue-500',
      progressBg: 'bg-teal-500',
      textColor: 'text-teal-600',
    };
  }
  if (clean.includes('on_time') || clean.includes('on_schedule')) {
    return {
      label: 'On Schedule',
      bg: 'bg-teal-100',
      text: 'text-teal-800',
      dot: 'bg-teal-500',
      border: 'border-teal-500',
      progressBg: 'bg-teal-500',
      textColor: 'text-teal-600',
    };
  }
  if (clean.includes('blocked')) {
    return {
      label: 'Blocked',
      bg: 'bg-purple-100',
      text: 'text-purple-700',
      dot: 'bg-purple-500',
      border: 'border-purple-500',
      progressBg: 'bg-purple-500',
      textColor: 'text-purple-600',
    };
  }
  return {
    label: 'Planned',
    bg: 'bg-slate-100',
    text: 'text-slate-600',
    dot: 'bg-slate-300',
    border: 'border-slate-300',
    progressBg: 'bg-slate-300',
    textColor: 'text-slate-500',
  };
}

export function getStatusBadge(status) {
  return getStatusBadgeConfig(status);
}

export function getProgressBarColor(status) {
  const config = getStatusBadgeConfig(status);
  return config.progressBg;
}

