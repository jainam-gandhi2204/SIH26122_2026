import React from 'react';

/**
 * Metric cards grid displaying dynamic Overall Progress donut, Schedule Variance, Status breakdown, and AI Confidence.
 * Preserves the Stitch visual design and derives all values strictly from backend data.
 */
export default function MetricCardsGrid({
  overallProgress = { actual: 0, planned: 0 },
  criticalDeviation = null,
  statusCounts = { completed: 0, in_progress: 0, delayed: 0, at_risk: 0, planned: 0, total: 0 },
  aiMetrics = { avgConfidence: 0, totalLinked: 0, totalTasks: 0, hasExtractions: false },
  isLoading = false,
}) {
  if (isLoading) {
    return (
      <section aria-label="Key Project Metrics" className="grid grid-cols-4 gap-4">
        {[1, 2, 3, 4].map((i) => (
          <div
            key={i}
            className="bg-white rounded-xl p-4 border border-slate-200 shadow-[0_1px_3px_rgba(0,0,0,0.03)] flex flex-col justify-between h-[126px] animate-pulse"
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className="w-7 h-7 bg-slate-100 rounded-lg" />
                <div className="w-24 h-4 bg-slate-100 rounded" />
              </div>
              <div className="w-12 h-4 bg-slate-100 rounded" />
            </div>
            <div className="flex items-baseline gap-2">
              <div className="w-16 h-7 bg-slate-100 rounded" />
              <div className="w-20 h-4 bg-slate-100 rounded" />
            </div>
            <div className="w-full bg-slate-100 h-2 rounded" />
          </div>
        ))}
      </section>
    );
  }

  const actualPct = Math.round(overallProgress.actual ?? 0);
  const plannedPct = Math.round(overallProgress.planned ?? 0);
  const variancePct = actualPct - plannedPct;
  const isCritical = Boolean(criticalDeviation && criticalDeviation.slipDays > 0) || statusCounts.delayed > 0;
  const hasTasks = statusCounts.total > 0;

  // Donut SVG circumference calculation for 36x36 (radius = 15.9155 -> perimeter = 100)
  const actualDash = `${Math.min(100, Math.max(0, actualPct))}, 100`;
  const plannedDash = `${Math.min(100, Math.max(0, plannedPct))}, 100`;

  return (
    <section aria-label="Key Project Metrics" className="grid grid-cols-4 gap-4">
      {/* Metric 1: Overall Progress */}
      <div className="bg-white rounded-xl p-4 border border-slate-200 shadow-[0_1px_3px_rgba(0,0,0,0.03)] flex flex-col justify-between h-[126px]">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="p-1.5 bg-sky-50 text-sky-600 rounded-lg">
              <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                <path d="M2 11a1 1 0 011-1h2a1 1 0 011 1v5a1 1 0 01-1 1H3a1 1 0 01-1-1v-5zM8 7a1 1 0 011-1h2a1 1 0 011 1v9a1 1 0 01-1 1H9a1 1 0 01-1-1V7zM14 4a1 1 0 011-1h2a1 1 0 011 1v12a1 1 0 01-1 1h-2a1 1 0 01-1-1V4z" />
              </svg>
            </span>
            <span className="font-bold text-slate-700 text-[13px]">Overall Progress</span>
          </div>
          <span className="text-slate-400 text-[11px] font-semibold">
            {!hasTasks
              ? 'No tasks'
              : actualPct >= plannedPct
              ? 'On Schedule'
              : `${Math.abs(variancePct)}% behind`}
          </span>
        </div>

        <div className="flex items-center justify-between px-2 pt-1">
          {/* Radial Donut Progress Indicator */}
          <div className="relative w-16 h-16 flex items-center justify-center shrink-0">
            <svg className="w-16 h-16 transform -rotate-90" viewBox="0 0 36 36">
              {/* Background Circle */}
              <path
                className="text-slate-100"
                d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
                fill="none"
                stroke="currentColor"
                strokeWidth="3.8"
              />
              {/* Planned Gray Arc */}
              <path
                className="text-slate-300"
                d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
                fill="none"
                stroke="currentColor"
                strokeDasharray={plannedDash}
                strokeWidth="3.8"
              />
              {/* Actual Teal Arc */}
              <path
                className="text-teal-500"
                d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
                fill="none"
                stroke="currentColor"
                strokeDasharray={actualDash}
                strokeLinecap="round"
                strokeWidth="3.8"
              />
            </svg>
            <div className="absolute font-extrabold text-slate-800 text-[14px]">
              {actualPct}%
            </div>
          </div>

          {/* Legend & Sub-Bar */}
          <div className="space-y-1 text-[11px] flex-1 pl-4">
            <div className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-teal-500 shrink-0" />
              <span className="text-slate-600">
                Actual <strong className="text-slate-800 font-bold">{actualPct}%</strong>
              </span>
            </div>
            <div className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-slate-400 shrink-0" />
              <span className="text-slate-600">
                Planned <strong className="text-slate-800 font-bold">{plannedPct}%</strong>
              </span>
            </div>
            <div className="w-full bg-slate-200 h-1.5 rounded-full overflow-hidden mt-1 flex">
              <div className="bg-teal-500 h-full" style={{ width: `${Math.min(100, actualPct)}%` }} />
              {plannedPct > actualPct && (
                <div
                  className="bg-slate-300 h-full"
                  style={{ width: `${Math.min(100 - actualPct, plannedPct - actualPct)}%` }}
                />
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Metric 2: Schedule Variance */}
      <div className="bg-white rounded-xl p-4 border border-slate-200 shadow-[0_1px_3px_rgba(0,0,0,0.03)] flex flex-col justify-between h-[126px]">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span
              className={`p-1.5 rounded-lg ${
                isCritical ? 'bg-red-50 text-red-500' : 'bg-emerald-50 text-emerald-600'
              }`}
            >
              <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                <path d="M2 11a1 1 0 011-1h2a1 1 0 011 1v5a1 1 0 01-1 1H3a1 1 0 01-1-1v-5zM8 7a1 1 0 011-1h2a1 1 0 011 1v9a1 1 0 01-1 1H9a1 1 0 01-1-1V7zM14 4a1 1 0 011-1h2a1 1 0 011 1v12a1 1 0 01-1 1h-2a1 1 0 01-1-1V4z" />
              </svg>
            </span>
            <span className="font-bold text-slate-700 text-[13px]">Schedule Variance</span>
          </div>
          <span
            className={`px-2 py-0.5 rounded text-[10px] font-extrabold uppercase tracking-wider ${
              isCritical ? 'bg-red-100 text-red-600' : 'bg-emerald-100 text-emerald-700'
            }`}
          >
            {isCritical ? 'CRITICAL' : 'ON TRACK'}
          </span>
        </div>

        <div>
          <div className="flex items-baseline gap-2">
            <span className={`text-2xl font-black tracking-tight ${isCritical ? 'text-red-600' : 'text-emerald-600'}`}>
              {!hasTasks ? '0%' : variancePct > 0 ? `+${variancePct}%` : `${variancePct}%`}
            </span>
            <span className={`text-[13px] font-bold ${isCritical ? 'text-red-600' : 'text-emerald-700'}`}>
              {criticalDeviation && criticalDeviation.slipDays > 0
                ? `(-${criticalDeviation.slipDays} Day Slip)`
                : '(Aligned)'}
            </span>
          </div>
          <p className="text-[11px] text-slate-500 mt-1 flex items-center gap-1 leading-tight truncate">
            {isCritical && criticalDeviation ? (
              <>
                <span className="text-red-500 font-bold shrink-0">↓</span>
                <span className="truncate">
                  <strong className="text-slate-700 font-semibold">{criticalDeviation.task_id}</strong> delay due to{' '}
                  {criticalDeviation.reason}
                </span>
              </>
            ) : !hasTasks ? (
              <span className="text-slate-400">No scheduled activities</span>
            ) : (
              <span className="text-emerald-600 font-medium">All activities aligned with baseline</span>
            )}
          </p>
        </div>
      </div>

      {/* Metric 3: Schedule Status */}
      <div className="bg-white rounded-xl p-4 border border-slate-200 shadow-[0_1px_3px_rgba(0,0,0,0.03)] flex flex-col justify-between h-[126px]">
        <div className="flex items-center gap-2">
          <span className="p-1.5 bg-blue-50 text-blue-600 rounded-lg">
            <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
              <path d="M9 2a1 1 0 000 2h2a1 1 0 100-2H9z" />
              <path
                clipRule="evenodd"
                d="M4 5a2 2 0 012-2 3 3 0 003 3h2a3 3 0 003-3 2 2 0 012 2v11a2 2 0 01-2 2H6a2 2 0 01-2-2V5zm3 4a1 1 0 000 2h.01a1 1 0 100-2H7zm3 0a1 1 0 000 2h3a1 1 0 100-2h-3zm-3 4a1 1 0 100 2h.01a1 1 0 100-2H7zm3 0a1 1 0 100 2h3a1 1 0 100-2h-3z"
                fillRule="evenodd"
              />
            </svg>
          </span>
          <span className="font-bold text-slate-700 text-[13px]">Schedule Status</span>
        </div>

        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-black text-slate-900">{statusCounts.total}</span>
          <span className="text-[12px] font-semibold text-slate-600">
            {statusCounts.total === 1 ? 'Activity' : 'Activities'}
          </span>
        </div>

        <div className="grid grid-cols-2 gap-x-2 gap-y-1 text-[11px]">
          <div className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-teal-500 shrink-0" />
            <span className="text-slate-600 font-medium">{statusCounts.completed} Completed</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-blue-600 shrink-0" />
            <span className="text-slate-600 font-medium">{statusCounts.in_progress} In Progress</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-red-500 shrink-0" />
            <span className="text-slate-600 font-medium">{statusCounts.delayed} Delayed</span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-amber-500 shrink-0" />
            <span className="text-slate-600 font-medium">{statusCounts.at_risk} At Risk</span>
          </div>
        </div>
      </div>

      {/* Metric 4: AI Extraction & Linking */}
      <div className="bg-white rounded-xl p-4 border border-slate-200 shadow-[0_1px_3px_rgba(0,0,0,0.03)] flex flex-col justify-between h-[126px]">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="p-1.5 bg-teal-50 text-teal-600 rounded-lg">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
              </svg>
            </span>
            <span className="font-bold text-slate-700 text-[13px]">AI Extraction &amp; Linking</span>
          </div>
          <span
            className={`px-2 py-0.5 rounded text-[10px] font-extrabold uppercase tracking-wider ${
              aiMetrics.hasExtractions ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-600'
            }`}
          >
            {aiMetrics.hasExtractions ? 'ACTIVE' : 'STANDBY'}
          </span>
        </div>

        <div>
          <div className="flex items-baseline gap-1.5">
            <span className="text-2xl font-black text-slate-900">{aiMetrics.avgConfidence}%</span>
            <span className="text-[12px] font-bold text-slate-700">Avg Confidence</span>
          </div>
          <p className="text-[11px] text-slate-500 mt-1 leading-tight truncate">
            {aiMetrics.totalLinked > 0
              ? `High-Confidence Multimodal Site Ingestion (${aiMetrics.totalLinked} of ${aiMetrics.totalTasks} linked)`
              : 'Awaiting high-confidence site update matches'}
          </p>
          <div className="w-full bg-slate-100 h-1.5 rounded-full overflow-hidden mt-1.5">
            <div
              className="bg-teal-500 h-full rounded-full transition-all duration-500"
              style={{ width: `${aiMetrics.avgConfidence}%` }}
            />
          </div>
        </div>
      </div>
    </section>
  );
}
