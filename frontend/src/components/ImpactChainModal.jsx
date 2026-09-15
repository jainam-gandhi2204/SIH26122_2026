import React, { useEffect, useState } from 'react';
import { fetchTaskImpact } from '../api/client';
import { formatDateRange, getStatusBadge } from '../utils/formatters';

/**
 * Modal dialog displaying the downstream CPM impact chain for a delayed or selected task.
 */
export default function ImpactChainModal({ isOpen, onClose, taskId, allTasks = [] }) {
  const [impactData, setImpactData] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (isOpen && taskId) {
      setIsLoading(true);
      setError(null);
      fetchTaskImpact(taskId)
        .then((data) => {
          setImpactData(data);
        })
        .catch((err) => {
          console.warn('Impact chain fetch failed, falling back to client derivation:', err);
          // Fallback client derivation if endpoint returns empty or fails
          const rootTask = allTasks.find((t) => t.task_id === taskId);
          const downstream = allTasks.filter((t) => t.task_id > taskId);
          setImpactData({
            task_id: taskId,
            task_name: rootTask?.task_name || 'Selected Activity',
            slip_days: rootTask?.status?.toLowerCase().includes('delay') ? 1 : 0,
            impacted_tasks: downstream.map((d) => ({
              task_id: d.task_id,
              task_name: d.task_name,
              planned_start: d.planned_start,
              planned_end: d.planned_end,
              status: 'at_risk',
              cascade_slip_days: 1,
            })),
          });
        })
        .finally(() => setIsLoading(false));
    } else {
      setImpactData(null);
    }
  }, [isOpen, taskId, allTasks]);

  if (!isOpen) return null;

  const rootTask = impactData?.task || allTasks.find((t) => t.task_id === taskId || t.source_task_id === taskId);
  const rootTaskId = rootTask?.source_task_id || rootTask?.task_id || taskId;
  const rootTaskName = rootTask?.activity || rootTask?.task_name || impactData?.task_name || 'Activity';
  const slipDays = rootTask?.deviation?.slip_days ?? impactData?.slip_days ?? rootTask?.delay_days ?? 0;
  const isCompleted = (rootTask?.status || '').toLowerCase() === 'completed' || (rootTask?.progress_percent ?? 0) >= 100;
  const isDelayed = slipDays > 0 || (rootTask?.status || '').toLowerCase() === 'delayed';
  const rawDownstream = impactData?.downstream || impactData?.impacted_tasks || impactData?.downstream_tasks || [];
  const impactedTasks = rawDownstream.map((t) => ({
    task_id: t.source_task_id || t.task_id,
    task_name: t.activity || t.task_name,
    planned_start: t.planned_start,
    planned_end: t.planned_end,
    status: t.schedule_health || t.status || (t.at_risk ? 'at_risk' : 'on_time'),
    cascade_slip_days: t.cascade_slip_days ?? 0,
    at_risk: Boolean(t.at_risk || (t.cascade_slip_days && t.cascade_slip_days > 0)),
    risk_reason: t.risk_reason,
  }));

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/60 backdrop-blur-xs transition-opacity animate-in fade-in">
      <div className="bg-white rounded-2xl shadow-2xl border border-slate-200 w-full max-w-2xl overflow-hidden flex flex-col max-h-[90vh]">
        {/* Modal Header */}
        <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between bg-[#091628] text-white">
          <div className="flex items-center gap-2.5">
            <div
              className={`w-8 h-8 rounded-lg flex items-center justify-center ${
                isDelayed
                  ? 'bg-red-500/20 border border-red-500/40 text-red-400'
                  : 'bg-emerald-500/20 border border-emerald-500/40 text-emerald-400'
              }`}
            >
              {isDelayed ? (
                <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                  <path
                    clipRule="evenodd"
                    d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z"
                    fillRule="evenodd"
                  />
                </svg>
              ) : (
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path d="M5 13l4 4L19 7" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" />
                </svg>
              )}
            </div>
            <div>
              <h3 className="font-extrabold text-[15px] leading-tight text-white">
                Downstream CPM Impact Chain Analysis
              </h3>
              <p className="text-[11px] text-slate-300">
                Critical path propagation for activity <span className="font-bold text-cyan-300">{rootTaskId}</span>
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="w-8 h-8 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 flex items-center justify-center transition cursor-pointer"
          >
            ✕
          </button>
        </div>

        {/* Modal Body */}
        <div className="p-6 overflow-y-auto space-y-5">
          {isLoading ? (
            <div className="py-12 text-center text-slate-500 text-[13px] flex flex-col items-center gap-2">
              <div className="w-6 h-6 border-2 border-teal-600 border-t-transparent rounded-full animate-spin" />
              <span>Analyzing downstream dependencies...</span>
            </div>
          ) : (
            <>
              {/* Root Cause Card */}
              {isCompleted && slipDays === 0 ? (
                <div className="p-4 rounded-xl bg-emerald-50/70 border border-emerald-200">
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-[11px] font-bold text-emerald-700 uppercase tracking-wider">
                      Activity Status: Completed
                    </span>
                    <span className="px-2 py-0.5 rounded text-[10.5px] font-extrabold bg-emerald-100 text-emerald-700">
                      0d Slip (On Schedule)
                    </span>
                  </div>
                  <div className="text-[15px] font-extrabold text-slate-900">
                    {rootTaskId} - {rootTaskName}
                  </div>
                  <p className="text-[12px] text-slate-600 mt-1">
                    Activity completed on schedule with zero critical path variance. Downstream dependencies are clear to proceed as planned.
                  </p>
                </div>
              ) : slipDays === 0 ? (
                <div className="p-4 rounded-xl bg-slate-50 border border-slate-200">
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-[11px] font-bold text-slate-700 uppercase tracking-wider">
                      Activity Status: On Track
                    </span>
                    <span className="px-2 py-0.5 rounded text-[10.5px] font-extrabold bg-emerald-100 text-emerald-700">
                      0d Slip (Aligned)
                    </span>
                  </div>
                  <div className="text-[15px] font-extrabold text-slate-900">
                    {rootTaskId} - {rootTaskName}
                  </div>
                  <p className="text-[12px] text-slate-600 mt-1">
                    Activity is progressing on schedule aligned with planned milestones.
                  </p>
                </div>
              ) : (
                <div className="p-4 rounded-xl bg-red-50/70 border border-red-200">
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-[11px] font-bold text-red-700 uppercase tracking-wider">
                      Root Delay Activity
                    </span>
                    <span className="px-2 py-0.5 rounded text-[10.5px] font-extrabold bg-red-100 text-red-700">
                      -{slipDays} Day Slip
                    </span>
                  </div>
                  <div className="text-[15px] font-extrabold text-slate-900">
                    {rootTaskId} - {rootTaskName}
                  </div>
                  <p className="text-[12px] text-slate-600 mt-1">
                    {rootTask?.delay_reason
                      ? `Cause: ${rootTask.delay_reason}. Schedule variance is directly pushing forward successor activity earliest start dates along the Critical Path.`
                      : 'Schedule variance is directly pushing forward successor activity earliest start dates along the Critical Path.'}
                  </p>
                </div>
              )}

              {/* Impact Cascade Chain */}
              <div>
                <h4 className="text-[12px] font-bold text-slate-700 uppercase tracking-wider mb-2 flex items-center gap-2">
                  <span>Downstream Dependent Activities ({impactedTasks.length})</span>
                </h4>

                {impactedTasks.length === 0 ? (
                  <div className="p-4 text-center text-slate-500 text-[12px] bg-slate-50 rounded-lg">
                    No successor activities directly affected on the Critical Path.
                  </div>
                ) : (
                  <div className="space-y-2 relative">
                    {/* Connecting line */}
                    <div className="absolute left-4 top-3 bottom-3 w-0.5 bg-slate-200 z-0" />

                    {impactedTasks.map((task) => {
                      const badge = getStatusBadge(task.status || 'at_risk');
                      return (
                        <div
                          key={task.task_id}
                          className="relative z-10 flex items-center justify-between p-3 rounded-lg border border-slate-200 bg-white hover:bg-slate-50/80 transition pl-10"
                        >
                          {/* Dot marker */}
                          <div
                            className={`absolute left-2.5 w-3 h-3 rounded-full border-2 border-white shadow-xs ${
                              task.cascade_slip_days > 0 ? 'bg-red-500' : 'bg-emerald-400'
                            }`}
                          />

                          <div>
                            <div className="flex items-center gap-2">
                              <span className="font-bold text-[12.5px] text-slate-900">
                                {task.task_id}
                              </span>
                              <span className="font-medium text-[12.5px] text-slate-800">
                                {task.task_name}
                              </span>
                            </div>
                            <div className="text-[11px] text-slate-500">
                              {formatDateRange(task.planned_start, task.planned_end)}
                            </div>
                          </div>

                          <div className="text-right">
                            <span className={`px-2 py-0.5 rounded-full text-[10.5px] font-semibold ${badge.bg} ${badge.text}`}>
                              {badge.label}
                            </span>
                            {task.cascade_slip_days > 0 ? (
                              <div className="text-[10px] text-red-600 font-semibold mt-0.5">
                                Estimated +{task.cascade_slip_days}d delay
                              </div>
                            ) : (
                              <div className="text-[10px] text-emerald-600 font-semibold mt-0.5">
                                On Schedule (+0d)
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>

              {/* Recommended Mitigation */}
              <div className="p-4 rounded-xl bg-slate-50 border border-slate-200">
                <h4 className="text-[12px] font-bold text-slate-800 mb-1 flex items-center gap-1.5">
                  <svg className="w-4 h-4 text-teal-600" fill="currentColor" viewBox="0 0 20 20">
                    <path
                      clipRule="evenodd"
                      d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z"
                      fillRule="evenodd"
                    />
                  </svg>
                  <span>Recommended Planner Actions</span>
                </h4>
                {isCompleted ? (
                  <ul className="text-[11.5px] text-slate-600 space-y-1 list-disc list-inside mt-1.5">
                    <li>Verify QA/QC sign-off records and engineering documentation for completed {rootTaskId}.</li>
                    <li>Authorize immediate site handoff for successor activity {impactedTasks[0]?.task_id || 'next phase'}.</li>
                    <li>Confirm material staging and crew readiness for downstream milestone execution.</li>
                  </ul>
                ) : isDelayed ? (
                  <ul className="text-[11.5px] text-slate-600 space-y-1 list-disc list-inside mt-1.5">
                    <li>Deploy additional resources to accelerate remaining {Math.max(0, 100 - Math.round(rootTask?.progress_percent || 0))}% on {rootTaskId}.</li>
                    <li>Pre-stage materials and equipment for successor activity to fast-track handoff.</li>
                    <li>{rootTask?.delay_reason ? `Address root disruption: ${rootTask.delay_reason}.` : 'Re-baseline downstream target milestones if disruption persists beyond 24 hours.'}</li>
                  </ul>
                ) : (
                  <ul className="text-[11.5px] text-slate-600 space-y-1 list-disc list-inside mt-1.5">
                    <li>Continue monitoring execution against planned milestones ({Math.round(rootTask?.progress_percent || 0)}% completed).</li>
                    <li>Ensure material logistics and equipment readiness for successor handoffs.</li>
                    <li>Maintain daily site update reporting to preserve schedule alignment.</li>
                  </ul>
                )}
              </div>
            </>
          )}
        </div>

        {/* Modal Footer */}
        <div className="px-6 py-3.5 border-t border-slate-100 bg-slate-50 flex items-center justify-end">
          <button
            onClick={onClose}
            className="px-4 py-2 bg-slate-800 hover:bg-slate-900 text-white rounded-lg text-[12px] font-bold transition cursor-pointer"
          >
            Close Analysis
          </button>
        </div>
      </div>
    </div>
  );
}

