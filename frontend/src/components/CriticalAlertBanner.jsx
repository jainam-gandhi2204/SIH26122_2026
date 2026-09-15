import React from 'react';

/**
 * Critical path alert banner warning of schedule slips and linking to impact chain.
 * Strictly uses real backend deviation data, real downstream counts, and real delay reasons.
 */
export default function CriticalAlertBanner({
  criticalDeviation,
  atRiskCount = 0,
  atRiskRange = '',
  atRiskDescription = '',
  tasksCount = 0,
  isLoading = false,
  onViewImpactChain,
}) {
  if (isLoading) {
    return (
      <section className="bg-slate-100 border border-slate-200 rounded-xl p-3.5 flex items-center justify-between shadow-xs animate-pulse h-[68px]">
        <div className="flex items-center gap-3 w-full">
          <div className="w-8 h-8 rounded-lg bg-slate-200 shrink-0" />
          <div className="space-y-1.5 flex-1">
            <div className="h-4 w-1/3 bg-slate-200 rounded" />
            <div className="h-3 w-2/3 bg-slate-200 rounded" />
          </div>
        </div>
      </section>
    );
  }

  if (tasksCount === 0) {
    return (
      <section className="bg-slate-50 border border-slate-200 rounded-xl p-3.5 flex items-center justify-between shadow-xs">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-slate-200 text-slate-500 flex items-center justify-center shrink-0">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
            </svg>
          </div>
          <div>
            <h3 className="font-extrabold text-[13.5px] text-slate-800">
              No Schedule Activities Loaded
            </h3>
            <p className="text-[12px] text-slate-500 mt-0.5">
              Import a master schedule spreadsheet to begin automated deviation tracking and Critical Path analysis.
            </p>
          </div>
        </div>
      </section>
    );
  }

  const slipDays = criticalDeviation?.slipDays ?? 0;

  if ((!criticalDeviation || slipDays <= 0) && atRiskCount === 0) {
    return (
      <section className="bg-emerald-50/70 border border-emerald-200/90 rounded-xl p-3.5 flex items-center justify-between shadow-xs">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-emerald-100 border border-emerald-200 text-emerald-700 flex items-center justify-center shrink-0">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path d="M5 13l4 4L19 7" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" />
            </svg>
          </div>
          <div>
            <h3 className="font-extrabold text-[13.5px] text-emerald-950">
              Schedule Health: On Track
            </h3>
            <p className="text-[12px] text-slate-600 mt-0.5">
              All {tasksCount} activities are currently progressing aligned with baseline planned milestones.
            </p>
          </div>
        </div>
      </section>
    );
  }

  const taskId = criticalDeviation?.task_id || 'Schedule Task';
  const taskName = criticalDeviation?.task_name || '';
  const progressPercent = criticalDeviation?.progress_percent;
  const reason = criticalDeviation?.reason;

  return (
    <section className="bg-rose-50/70 border border-rose-200/90 rounded-xl p-3.5 flex items-center justify-between shadow-xs">
      <div className="flex items-start gap-3 max-w-4xl">
        {/* Alert Warning Icon */}
        <div className="w-8 h-8 rounded-lg bg-red-100 border border-red-200 text-red-600 flex items-center justify-center shrink-0 mt-0.5">
          <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
            <path
              clipRule="evenodd"
              d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z"
              fillRule="evenodd"
            />
          </svg>
        </div>

        {/* Alert Details */}
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="font-extrabold text-[13.5px] text-red-950">
              Critical Path Delay: {taskId} {taskName} (-{slipDays} Day Slip)
            </h3>
            {atRiskCount > 0 && (
              <span className="px-2 py-0.5 rounded-full bg-red-100/90 border border-red-200 text-[11px] font-semibold text-red-700">
                {atRiskCount} Downstream Task{atRiskCount === 1 ? '' : 's'} At Risk{atRiskRange ? ` (${atRiskRange})` : ''}
              </span>
            )}
          </div>
          <p className="text-[12px] text-slate-600 mt-1 leading-normal">
            {reason ? `${reason} caused a` : 'Detected a'} {slipDays}-day critical path delay on{' '}
            <strong className="text-slate-800 font-semibold">{taskId} {taskName}</strong>
            {progressPercent !== undefined ? ` (progress at ${progressPercent}%)` : ''}.{' '}
            {atRiskDescription ||
              'Downstream activities along the Critical Path are at risk unless planner mitigation is taken.'}
          </p>
        </div>
      </div>

      {/* Action Button */}
      <button
        onClick={() => onViewImpactChain && onViewImpactChain(taskId)}
        className="shrink-0 ml-4 px-3.5 py-2 bg-[#0c1e34] hover:bg-[#132c4a] text-white rounded-lg font-semibold text-[12px] flex items-center gap-1.5 transition shadow-sm cursor-pointer"
      >
        <span>View Impact Chain</span>
        <span>→</span>
      </button>
    </section>
  );
}
