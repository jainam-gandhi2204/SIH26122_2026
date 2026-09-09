import React from 'react';
import { formatDate, formatDateRange, getStatusBadge, getProgressBarColor } from '../utils/formatters';

/**
 * Project schedule activities table comparing baseline vs actuals.
 * Dynamically rendered from real backend data with planned vs actual distinctions.
 */
export default function ProjectScheduleTable({
  tasks = [],
  isLoading = false,
  error = null,
  onSelectTask,
  searchQuery = '',
  onOpenImportSchedule,
}) {
  const filteredTasks = tasks.filter((task) => {
    if (!searchQuery) return true;
    const q = searchQuery.toLowerCase();
    return (
      (task.task_id && task.task_id.toLowerCase().includes(q)) ||
      (task.task_name && task.task_name.toLowerCase().includes(q)) ||
      (task.location && task.location.toLowerCase().includes(q)) ||
      (task.status && task.status.toLowerCase().includes(q)) ||
      (task.schedule_health && task.schedule_health.toLowerCase().includes(q))
    );
  });

  // Extract dynamic month/year from tasks for header
  const getHeaderPeriod = () => {
    const taskWithDate = tasks.find((t) => t.planned_start);
    if (!taskWithDate || !taskWithDate.planned_start) return 'Schedule';
    try {
      const d = new Date(taskWithDate.planned_start);
      if (isNaN(d.getTime())) return 'Schedule';
      return d.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
    } catch {
      return 'Schedule';
    }
  };

  const getBorderColor = (status) => {
    const s = (status || '').toLowerCase();
    if (s.includes('delay')) return 'border-red-500';
    if (s.includes('risk')) return 'border-amber-500';
    if (s.includes('complete')) return 'border-emerald-500';
    if (s.includes('progress') || s.includes('on_time')) return 'border-blue-500';
    return 'border-slate-300';
  };

  const getTaskIdColor = (status) => {
    const s = (status || '').toLowerCase();
    if (s.includes('delay')) return 'text-red-600';
    if (s.includes('risk')) return 'text-amber-600';
    if (s.includes('complete')) return 'text-teal-600';
    if (s.includes('progress') || s.includes('on_time')) return 'text-blue-600';
    return 'text-slate-700';
  };

  return (
    <div className="bg-white rounded-xl border border-slate-200 shadow-[0_1px_3px_rgba(0,0,0,0.03)] p-4 flex flex-col justify-between h-full">
      <div>
        {/* Table Header */}
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <span className="p-1.5 bg-blue-50 text-blue-600 rounded-lg">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path
                  d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth="2"
                />
              </svg>
            </span>
            <div>
              <h2 className="font-extrabold text-[14px] text-slate-800">
                Project Schedule ({getHeaderPeriod()})
              </h2>
              <p className="text-[11px] text-slate-500 font-medium">
                Baseline vs Actual Progress • {filteredTasks.length}{' '}
                {filteredTasks.length === 1 ? 'Activity' : 'Activities'}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2.5">
            {onOpenImportSchedule && (
              <button
                type="button"
                onClick={onOpenImportSchedule}
                className="inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-semibold text-blue-700 bg-blue-50 hover:bg-blue-100 border border-blue-200/80 rounded-lg transition cursor-pointer"
                title="Import master schedule CSV"
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path
                    d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth="2"
                  />
                </svg>
                Import Schedule
              </button>
            )}
            <span className="text-[11px] font-semibold text-slate-400 flex items-center gap-1">
              <span>Click row for Impact Chain</span>
              <span className="text-slate-300">→</span>
            </span>
          </div>
        </div>

        {/* Schedule Table */}
        <div className="overflow-x-auto">
          <table className="w-full text-left text-[12px] border-collapse">
            <thead>
              <tr className="border-y border-slate-100 text-slate-500 text-[11px] font-semibold">
                <th className="py-2 px-2 font-semibold">ID</th>
                <th className="py-2 px-2 font-semibold">Activity Name</th>
                <th className="py-2 px-2 font-semibold">Planned &amp; Actual Dates</th>
                <th className="py-2 px-2 font-semibold">Actual Progress</th>
                <th className="py-2 px-2 font-semibold text-right">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {isLoading ? (
                // Loading Skeleton Rows
                Array.from({ length: 6 }).map((_, idx) => (
                  <tr key={`skeleton-${idx}`} className="animate-pulse">
                    <td className="py-3 px-2">
                      <div className="h-4 bg-slate-200 rounded w-10" />
                    </td>
                    <td className="py-3 px-2">
                      <div className="h-4 bg-slate-200 rounded w-36 mb-1" />
                      <div className="h-3 bg-slate-100 rounded w-20" />
                    </td>
                    <td className="py-3 px-2">
                      <div className="h-3.5 bg-slate-200 rounded w-32 mb-1" />
                      <div className="h-3 bg-slate-100 rounded w-24" />
                    </td>
                    <td className="py-3 px-2 w-36">
                      <div className="h-2 bg-slate-200 rounded-full w-24" />
                    </td>
                    <td className="py-3 px-2 text-right">
                      <div className="h-5 bg-slate-200 rounded-full w-16 ml-auto" />
                    </td>
                  </tr>
                ))
              ) : error ? (
                // Error State
                <tr>
                  <td colSpan={5} className="py-8 text-center text-rose-500 text-[12px]">
                    <p className="font-bold">Failed to load schedule activities.</p>
                    <p className="text-[11px] text-slate-400 mt-1">{error}</p>
                  </td>
                </tr>
              ) : filteredTasks.length === 0 ? (
                // Empty State
                <tr>
                  <td colSpan={5} className="py-10 text-center text-slate-400 text-[12px]">
                    <div className="flex flex-col items-center justify-center gap-1.5">
                      <svg className="w-8 h-8 text-slate-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path
                          d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 012-2h2a2 2 0 012 2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth="1.5"
                        />
                      </svg>
                      <p className="font-semibold text-slate-600">
                        {searchQuery ? `No activities match "${searchQuery}"` : 'No schedule activities found'}
                      </p>
                      <p className="text-[11px] text-slate-400">
                        {searchQuery
                          ? 'Try clearing or changing your search term.'
                          : 'Import a schedule CSV file or ingest site updates to populate.'}
                      </p>
                      {!searchQuery && onOpenImportSchedule && (
                        <button
                          type="button"
                          onClick={onOpenImportSchedule}
                          className="mt-2.5 inline-flex items-center gap-1.5 px-3 py-1.5 text-[11.5px] font-bold text-white bg-blue-600 hover:bg-blue-700 rounded-lg shadow-sm transition cursor-pointer"
                        >
                          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path
                              d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              strokeWidth="2"
                            />
                          </svg>
                          Import Project Schedule
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ) : (
                // Real Activities Rows
                filteredTasks.map((task) => {
                  const progress = Math.min(
                    100,
                    Math.max(0, Math.round(task.progress_percent ?? task.actual_progress ?? 0))
                  );
                  const badge = getStatusBadge(task.status);
                  const barColor = getProgressBarColor(task.status);
                  const idColor = getTaskIdColor(task.status);
                  const borderCol = getBorderColor(task.status);

                  // Distinguish planned vs actual dates
                  const hasActualDates = Boolean(task.actual_start || task.actual_end);
                  const hasDelay = Boolean(task.delay_days && task.delay_days > 0);

                  return (
                    <tr
                      key={task.task_id}
                      onClick={() => onSelectTask && onSelectTask(task.task_id)}
                      className="hover:bg-slate-50/80 transition cursor-pointer group"
                    >
                      {/* ID with status-colored border */}
                      <td className={`py-2.5 px-2 font-bold ${idColor} border-l-2 ${borderCol}`}>
                        {task.task_id}
                      </td>

                      {/* Activity Name & Location / Delay Reason */}
                      <td className="py-2.5 px-2 font-medium text-slate-800 group-hover:text-cyan-700 transition max-w-[200px]">
                        <div className="font-semibold text-slate-900 leading-tight truncate">
                          {task.task_name}
                        </div>
                        <div className="flex items-center gap-2 mt-0.5 text-[10.5px]">
                          {task.location && (
                            <span className="text-slate-400">{task.location}</span>
                          )}
                          {task.delay_reason && (
                            <span
                              className="text-rose-600 truncate max-w-[140px]"
                              title={task.delay_reason}
                            >
                              • {task.delay_reason}
                            </span>
                          )}
                        </div>
                      </td>

                      {/* Planned & Actual Dates */}
                      <td className="py-2.5 px-2 text-[11px]">
                        <div className="text-slate-600 font-medium">
                          {formatDateRange(task.planned_start, task.planned_end)}
                        </div>
                        {hasActualDates ? (
                          <div className="text-[10px] text-teal-700 font-semibold flex items-center gap-1 mt-0.5">
                            <span className="uppercase text-[9px] tracking-wider px-1 py-0.2 bg-teal-50 text-teal-700 rounded border border-teal-200/60">
                              Act
                            </span>
                            <span>
                              {task.actual_start ? formatDate(task.actual_start) : '—'} →{' '}
                              {task.actual_end ? formatDate(task.actual_end) : 'In Progress'}
                            </span>
                          </div>
                        ) : hasDelay ? (
                          <div className="text-[10px] text-rose-600 font-semibold flex items-center gap-1 mt-0.5">
                            <span className="px-1 py-0.2 bg-rose-50 text-rose-700 rounded border border-rose-200/60 text-[9px]">
                              +{task.delay_days}d slip
                            </span>
                          </div>
                        ) : (
                          <div className="text-[10px] text-slate-400">Baseline Target</div>
                        )}
                      </td>

                      {/* Actual Progress Bar */}
                      <td className="py-2.5 px-2 w-36">
                        <div className="flex items-center gap-2">
                          <div className="flex-1 bg-slate-100 h-2 rounded-full overflow-hidden">
                            <div
                              className={`${barColor} h-full rounded-full transition-all duration-300`}
                              style={{ width: `${progress}%` }}
                            />
                          </div>
                          <span
                            className={`text-[11px] font-bold ${
                              progress > 0 ? (hasDelay ? 'text-rose-600' : 'text-slate-800') : 'text-slate-400'
                            } w-8`}
                          >
                            {progress}%
                          </span>
                        </div>
                      </td>

                      {/* Status Badge */}
                      <td className="py-2.5 px-2 text-right">
                        <span
                          className={`px-2 py-0.5 rounded-full text-[10.5px] font-semibold ${badge.bg} ${badge.text}`}
                        >
                          {badge.label}
                        </span>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

