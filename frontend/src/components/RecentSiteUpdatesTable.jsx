import React from 'react';
import { formatTime, formatDate, formatRelativeDate, getStatusBadge, getProgressBarColor } from '../utils/formatters';

/**
 * Recent site updates feed showing AI extracted progress, confidence scores, and matched tasks.
 * Clearly distinguishes extracted facts from missing/unknown values.
 */
export default function RecentSiteUpdatesTable({
  updates = [],
  isLoading = false,
  error = null,
  onOpenNewUpdate,
  onSelectTask,
}) {
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
                  d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth="2"
                />
              </svg>
            </span>
            <div>
              <h2 className="font-extrabold text-[14px] text-slate-800">
                Recent Site Updates &amp; AI Extraction
              </h2>
              <p className="text-[11px] text-slate-500 font-medium">
                Field updates automatically extracted and matched to schedule activities
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[11px] font-semibold text-slate-400">
              {updates.length} {updates.length === 1 ? 'Recorded' : 'Recorded'}
            </span>
            <button
              onClick={onOpenNewUpdate}
              className="text-[11.5px] font-semibold text-blue-600 hover:text-blue-800 flex items-center gap-1 cursor-pointer"
            >
              + Ingest Update
            </button>
          </div>
        </div>

        {/* Feed Table */}
        <div className="overflow-x-auto">
          <table className="w-full text-left text-[11.5px] border-collapse">
            <thead>
              <tr className="border-y border-slate-100 text-slate-500 text-[10.5px] font-semibold">
                <th className="py-2 px-2 font-semibold">Time / ID</th>
                <th className="py-2 px-2 font-semibold">Activity</th>
                <th className="py-2 px-2 font-semibold">Extracted Update</th>
                <th className="py-2 px-2 font-semibold">Progress</th>
                <th className="py-2 px-2 font-semibold">Confidence</th>
                <th className="py-2 px-2 font-semibold text-right">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {isLoading ? (
                // Loading Skeleton Rows
                Array.from({ length: 5 }).map((_, idx) => (
                  <tr key={`feed-skel-${idx}`} className="animate-pulse">
                    <td className="py-3 px-2">
                      <div className="h-3.5 bg-slate-200 rounded w-16 mb-1" />
                      <div className="h-2.5 bg-slate-100 rounded w-12" />
                    </td>
                    <td className="py-3 px-2">
                      <div className="h-4 bg-slate-200 rounded w-10" />
                    </td>
                    <td className="py-3 px-2 max-w-[200px]">
                      <div className="h-3.5 bg-slate-200 rounded w-44 mb-1" />
                      <div className="h-2.5 bg-slate-100 rounded w-28" />
                    </td>
                    <td className="py-3 px-2">
                      <div className="h-2 bg-slate-200 rounded-full w-14" />
                    </td>
                    <td className="py-3 px-2">
                      <div className="h-4 bg-slate-200 rounded w-10" />
                    </td>
                    <td className="py-3 px-2 text-right">
                      <div className="h-4 bg-slate-200 rounded-full w-14 ml-auto" />
                    </td>
                  </tr>
                ))
              ) : error ? (
                // Error State
                <tr>
                  <td colSpan={6} className="py-8 text-center text-rose-500 text-[12px]">
                    <p className="font-bold">Failed to load site updates.</p>
                    <p className="text-[11px] text-slate-400 mt-1">{error}</p>
                  </td>
                </tr>
              ) : updates.length === 0 ? (
                // Empty State
                <tr>
                  <td colSpan={6} className="py-10 text-center text-slate-400 text-[12px]">
                    <div className="flex flex-col items-center justify-center gap-1.5">
                      <svg className="w-8 h-8 text-slate-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path
                          d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth="1.5"
                        />
                      </svg>
                      <p className="font-semibold text-slate-600">No site updates recorded yet.</p>
                      <button
                        onClick={onOpenNewUpdate}
                        className="mt-1 text-teal-600 hover:text-teal-700 font-semibold cursor-pointer underline text-[11px]"
                      >
                        Record the first field update →
                      </button>
                    </div>
                  </td>
                </tr>
              ) : (
                // Real Updates Rows
                updates.slice(0, 10).map((item) => {
                  const confVal = item.confidence_score ?? item.confidence;
                  const confPercent =
                    confVal !== null && confVal !== undefined && Number(confVal) > 0
                      ? Math.round(Number(confVal) <= 1.0 ? Number(confVal) * 100 : Number(confVal))
                      : null;

                  const progress =
                    item.progress_percent !== null && item.progress_percent !== undefined
                      ? Math.round(Number(item.progress_percent))
                      : null;

                  const badge = getStatusBadge(item.status);
                  const barColor = getProgressBarColor(item.status);
                  const taskId = item.matched_task_id;
                  const idColor = getTaskIdColor(item.status);
                  const hasDelay = item.delay_days && item.delay_days > 0;

                  return (
                    <tr key={item.id} className="hover:bg-slate-50/70 transition">
                      {/* Time / ID Column */}
                      <td className="py-2.5 px-2 text-slate-600 leading-tight">
                        <div className="flex items-center gap-1.5">
                          <svg className="w-3.5 h-3.5 text-slate-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path
                              d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              strokeWidth="2"
                            />
                          </svg>
                          <div>
                            <div className="font-medium text-slate-800">
                              {formatRelativeDate(item.reported_on || item.ingested_at)}
                            </div>
                            <div className="text-[10px] text-slate-400 flex items-center gap-1 mt-0.5">
                              <span>{item.ingested_at ? formatTime(item.ingested_at) : (item.reported_on ? formatDate(item.reported_on) : '—')}</span>
                              <span className="font-mono bg-slate-100 px-1 py-0.2 rounded text-[9.5px]">
                                {item.source_update_id || item.id?.slice(0, 6)}
                              </span>
                            </div>
                          </div>
                        </div>
                      </td>

                      {/* Matched Activity Column */}
                      <td className="py-2.5 px-2">
                        {taskId ? (
                          <button
                            onClick={() => onSelectTask && onSelectTask(taskId)}
                            className={`font-bold ${idColor} hover:underline cursor-pointer text-left block leading-tight`}
                            title={`Click to view impact chain for ${taskId}`}
                          >
                            {taskId}
                            {item.matched_task_name && (
                              <span className="block text-[10px] font-normal text-slate-400 truncate max-w-[120px]">
                                {item.matched_task_name}
                              </span>
                            )}
                          </button>
                        ) : item.is_processed ? (
                          <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-50 text-amber-700 border border-amber-200">
                            Unmatched
                          </span>
                        ) : (
                          <span className="text-slate-400 font-medium italic text-[10.5px]">
                            Pending AI
                          </span>
                        )}
                      </td>

                      {/* Extracted Update / Evidence Column */}
                      <td className="py-2.5 px-2 text-slate-700 max-w-[220px] leading-snug">
                        <div className="line-clamp-2 text-slate-800">
                          {item.raw_update || 'Site update recorded'}
                        </div>
                        <div className="text-[10px] text-slate-400 flex items-center gap-1.5 mt-0.5">
                          {item.location && <span>{item.location}</span>}
                          {item.source_reference && (
                            <span className="text-slate-300">• {item.source_reference}</span>
                          )}
                          {hasDelay && (
                            <span className="text-rose-600 font-bold">
                              • +{item.delay_days}d slip
                            </span>
                          )}
                        </div>
                      </td>

                      {/* Actual Progress Column (distinguishing fact from unstated) */}
                      <td className="py-2.5 px-2">
                        {progress !== null ? (
                          <div className="flex items-center gap-1.5">
                            <div className="w-12 bg-slate-100 h-1.5 rounded-full overflow-hidden">
                              <div
                                className={`${barColor} h-full rounded-full`}
                                style={{ width: `${Math.min(100, Math.max(0, progress))}%` }}
                              />
                            </div>
                            <span
                              className={`font-bold text-[10px] ${
                                hasDelay ? 'text-rose-600' : 'text-slate-700'
                              }`}
                            >
                              {progress}%
                            </span>
                          </div>
                        ) : (
                          <span className="text-slate-400 text-[10.5px] italic">Unstated</span>
                        )}
                      </td>

                      {/* Confidence Score Column */}
                      <td className="py-2.5 px-2">
                        {confPercent !== null ? (
                          <span
                            className={`px-1.5 py-0.5 rounded font-bold text-[10px] ${
                              confPercent >= 70
                                ? 'bg-emerald-50 text-teal-700'
                                : 'bg-amber-50 text-amber-700'
                            }`}
                          >
                            {confPercent}%
                          </span>
                        ) : (
                          <span className="text-slate-400 text-[10px]">—</span>
                        )}
                      </td>

                      {/* Status Column */}
                      <td className="py-2.5 px-2 text-right">
                        {item.status ? (
                          <span
                            className={`px-2 py-0.5 rounded-full text-[10px] font-semibold ${badge.bg} ${badge.text}`}
                          >
                            {badge.label}
                          </span>
                        ) : (
                          <span className="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-slate-100 text-slate-500">
                            Unassessed
                          </span>
                        )}
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

