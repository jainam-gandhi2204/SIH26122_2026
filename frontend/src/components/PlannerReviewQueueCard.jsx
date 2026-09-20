import React, { useState, useEffect } from 'react';
import { formatDateRange, formatRelativeTime } from '../utils/formatters';

/**
 * Format the review trigger reason into a concise, human-readable alert message.
 */
export function formatReviewReason(reason, confidence, threshold = 80) {
  const confPct = confidence !== null && confidence !== undefined ? Math.round(confidence) : 0;
  const threshPct = threshold !== null && threshold !== undefined ? Math.round(threshold) : 80;
  switch (reason) {
    case 'unmatched_and_low_confidence':
      return 'No schedule activity matched & AI confidence is low';
    case 'unmatched':
      return 'No schedule activity could be linked to this update';
    case 'low_confidence':
      return `AI match confidence (${confPct}%) is below ${threshPct}% threshold`;
    case 'ambiguous_match':
      return 'Multiple candidate activities matched at this location';
    default:
      return reason ? reason.replace(/_/g, ' ') : 'Requires planner validation';
  }
}

/**
 * Planner Review Queue card supporting human-in-the-loop validation, candidate selection,
 * match approval, and remapping/rejection.
 */
export default function PlannerReviewQueueCard({
  items = [],
  scheduleTasks = [],
  threshold = 80,
  isLoading = false,
  error = null,
  onApprove,
  onReject,
  onChangeMatch,
  isProcessing = false,
}) {
  const [currentIndex, setCurrentIndex] = useState(0);
  const [selectedTaskId, setSelectedTaskId] = useState('');
  const [showManualRemap, setShowManualRemap] = useState(false);
  const [manualTaskId, setManualTaskId] = useState('');

  // Keep currentIndex in bounds when items list changes
  useEffect(() => {
    if (currentIndex >= items.length && items.length > 0) {
      setCurrentIndex(items.length - 1);
    }
  }, [items.length, currentIndex]);

  const activeItem = items[currentIndex] || null;

  // When active item changes, set default selection to its suggested match or candidate
  useEffect(() => {
    if (activeItem) {
      const defaultId =
        activeItem.suggested_match?.source_task_id ||
        activeItem.suggested_match?.task_id ||
        activeItem.current_matched_task?.source_task_id ||
        activeItem.current_matched_task?.task_id ||
        activeItem.candidate_tasks?.[0]?.source_task_id ||
        activeItem.candidate_tasks?.[0]?.task_id ||
        '';
      setSelectedTaskId(defaultId);
      setManualTaskId(defaultId);
      setShowManualRemap(false);
    }
  }, [activeItem?.review_id, activeItem?.site_update_id]);

  const handleApprove = async () => {
    if (!activeItem) return;
    const targetTaskId = selectedTaskId || activeItem.suggested_match?.source_task_id || activeItem.suggested_match?.task_id;
    if (targetTaskId && targetTaskId !== activeItem.current_matched_task?.source_task_id) {
      await onChangeMatch?.(activeItem.review_id, targetTaskId);
    } else {
      await onApprove?.(activeItem.review_id, targetTaskId);
    }
  };

  const handleReject = async () => {
    if (!activeItem) return;
    await onReject?.(activeItem.review_id, 'Intentionally rejected by planner');
  };

  const handleManualRemapApply = async () => {
    if (!activeItem || !manualTaskId) return;
    setSelectedTaskId(manualTaskId);
    setShowManualRemap(false);
  };

  const handlePrev = () => {
    if (currentIndex > 0) setCurrentIndex(currentIndex - 1);
  };

  const handleNext = () => {
    if (currentIndex < items.length - 1) setCurrentIndex(currentIndex + 1);
  };

  // Loading Skeleton State
  if (isLoading) {
    return (
      <div className="bg-white rounded-xl border border-slate-200 shadow-[0_1px_3px_rgba(0,0,0,0.03)] p-4 flex flex-col justify-between h-full animate-pulse">
        <div>
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <div className="w-8 h-8 rounded-lg bg-slate-200" />
              <div>
                <div className="h-4 bg-slate-200 rounded w-40 mb-1" />
                <div className="h-3 bg-slate-100 rounded w-48" />
              </div>
            </div>
            <div className="h-5 bg-slate-200 rounded-full w-20" />
          </div>

          <div className="bg-slate-50 border border-slate-200 rounded-lg p-3 mb-3.5 space-y-2">
            <div className="h-3.5 bg-slate-200 rounded w-36" />
            <div className="h-10 bg-slate-200/60 rounded" />
          </div>

          <div className="space-y-2">
            <div className="h-12 bg-slate-100 rounded-lg" />
            <div className="h-12 bg-slate-100 rounded-lg" />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2.5 pt-3 mt-2 border-t border-slate-100">
          <div className="h-8 bg-slate-200 rounded-lg" />
          <div className="h-8 bg-slate-200 rounded-lg" />
        </div>
      </div>
    );
  }

  // Error State
  if (error && items.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-slate-200 shadow-[0_1px_3px_rgba(0,0,0,0.03)] p-4 flex flex-col justify-between h-full">
        <div>
          <div className="flex items-center gap-2 mb-3">
            <span className="p-1.5 bg-rose-50 text-rose-600 rounded-lg">⚠️</span>
            <h2 className="font-extrabold text-[14px] text-slate-800">Planner Review Queue</h2>
          </div>
          <div className="p-4 rounded-xl bg-rose-50 border border-rose-200 text-rose-700 text-[12px]">
            <p className="font-bold">Failed to load review queue items.</p>
            <p className="text-[11px] mt-1">{error}</p>
          </div>
        </div>
      </div>
    );
  }

  // Empty State: Review Queue Clear
  if (!activeItem || items.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-slate-200 shadow-[0_1px_3px_rgba(0,0,0,0.03)] p-4 flex flex-col justify-between h-full">
        <div>
          {/* Header */}
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <span className="p-1.5 bg-blue-50 text-blue-600 rounded-lg">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path
                    d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth="2"
                  />
                </svg>
              </span>
              <div>
                <h2 className="font-extrabold text-[14px] text-slate-800">
                  Planner Review Queue (Human-in-the-Loop)
                </h2>
                <p className="text-[11px] text-slate-500 font-medium">
                  Uncertain site updates requiring planner validation
                </p>
              </div>
            </div>
            <span className="px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-emerald-100 text-emerald-800">
              0 Pending
            </span>
          </div>

          {/* Empty State Card */}
          <div className="bg-slate-50/80 border border-dashed border-slate-200 rounded-xl p-8 text-center my-4 flex flex-col items-center justify-center">
            <div className="w-12 h-12 rounded-full bg-emerald-50 text-emerald-600 flex items-center justify-center mb-3 shadow-xs">
              <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path d="M5 13l4 4L19 7" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" />
              </svg>
            </div>
            <div className="font-extrabold text-slate-800 text-[14px] mb-1">
              Review Queue Clear
            </div>
            <p className="text-[12px] text-slate-500 max-w-xs leading-relaxed">
              All field updates have been successfully matched to schedule activities with high AI confidence.
            </p>
          </div>
        </div>

        <div className="text-[11px] text-slate-400 text-center pt-2 border-t border-slate-100">
          Automated schedule actual updates are active and protected.
        </div>
      </div>
    );
  }

  // Identify suggested match & candidates
  const suggested = activeItem.suggested_match || null;
  const rawCandidates = activeItem.candidate_tasks || [];

  // Filter out the suggested match from candidate list to prevent duplicate options
  const alternativeCandidates = rawCandidates.filter((c) => {
    if (!suggested) return true;
    return (
      c.source_task_id !== suggested.source_task_id &&
      c.task_id !== suggested.task_id
    );
  });

  const confScore = Math.round(Number(activeItem.confidence_score || 0));

  return (
    <div className="bg-white rounded-xl border border-slate-200 shadow-[0_1px_3px_rgba(0,0,0,0.03)] p-4 flex flex-col justify-between h-full">
      <div>
        {/* Header */}
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <span className="p-1.5 bg-blue-50 text-blue-600 rounded-lg">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path
                  d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth="2"
                />
              </svg>
            </span>
            <div>
              <h2 className="font-extrabold text-[14px] text-slate-800">
                Planner Review Queue (Human-in-the-Loop)
              </h2>
              <p className="text-[11px] text-slate-500 font-medium">
                Uncertain site updates requiring planner validation
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {items.length > 1 && (
              <div className="flex items-center gap-1">
                <button
                  onClick={handlePrev}
                  disabled={currentIndex === 0}
                  className="w-6 h-6 rounded bg-slate-100 hover:bg-slate-200 disabled:opacity-40 text-slate-700 text-[11px] font-bold flex items-center justify-center cursor-pointer transition"
                  title="Previous item"
                >
                  ‹
                </button>
                <span className="text-[11px] text-slate-500 font-semibold px-1">
                  {currentIndex + 1} of {items.length}
                </span>
                <button
                  onClick={handleNext}
                  disabled={currentIndex === items.length - 1}
                  className="w-6 h-6 rounded bg-slate-100 hover:bg-slate-200 disabled:opacity-40 text-slate-700 text-[11px] font-bold flex items-center justify-center cursor-pointer transition"
                  title="Next item"
                >
                  ›
                </button>
              </div>
            )}
            <span className="px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-amber-100 text-amber-800">
              {items.length} Pending
            </span>
          </div>
        </div>

        {/* Field Note Box */}
        <div className="bg-slate-50 border border-slate-200/80 rounded-lg p-3 mb-3">
          <div className="flex items-center justify-between text-[11.5px] mb-1.5">
            <div className="flex items-center gap-1.5 font-bold text-slate-800">
              <svg className="w-4 h-4 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path
                  d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth="2"
                />
              </svg>
              <span>Field Note • {activeItem.source_update_id || 'Update'}</span>
            </div>
            <span className="text-[11px] text-slate-500">
              {formatRelativeTime(activeItem.reported_on)}
            </span>
          </div>

          {/* Review Trigger Reason Alert */}
          <div className="text-[11px] text-amber-800 bg-amber-50/90 px-2.5 py-1 rounded border border-amber-200 mb-2 font-medium flex items-center justify-between">
            <span className="flex items-center gap-1">
              <span className="font-bold">⚠️ Review Trigger:</span>
              <span>{activeItem.review_trigger_message || formatReviewReason(activeItem.review_reason, confScore, activeItem.threshold ?? threshold)}</span>
            </span>
            {activeItem.location && (
              <span className="text-[10px] text-slate-500 font-semibold">{activeItem.location}</span>
            )}
          </div>

          {/* Raw Update Quotation */}
          <p className="text-[12px] italic text-slate-700 leading-relaxed bg-white/70 p-2 rounded border border-slate-200/60 mb-2">
            “{activeItem.raw_update}”
          </p>

          {/* Extracted Facts Summary Chip */}
          <div className="flex items-center gap-3 text-[10.5px] text-slate-500 pt-1 border-t border-slate-200/60">
            <span>
              Extracted Activity:{' '}
              <strong className="text-slate-800 font-semibold">
                {activeItem.extracted_activity || '—'}
              </strong>
            </span>
            <span>
              Progress:{' '}
              <strong className="text-slate-800 font-semibold">
                {activeItem.progress_percent !== null && activeItem.progress_percent !== undefined
                  ? `${activeItem.progress_percent}%`
                  : 'Unstated'}
              </strong>
            </span>
            <span>
              Status:{' '}
              <strong className="text-slate-800 font-semibold capitalize">
                {activeItem.status || 'Unstated'}
              </strong>
            </span>
          </div>
        </div>

        {/* Suggested Activity Match Header */}
        <div className="flex items-center justify-between mb-2">
          <span className="text-[11.5px] font-bold text-slate-700">Suggested Activity Match</span>
          <button
            onClick={() => setShowManualRemap(!showManualRemap)}
            className="text-[11px] font-semibold text-blue-600 hover:text-blue-800 cursor-pointer"
          >
            {showManualRemap ? 'Show Candidates' : 'Choose Other Task ▾'}
          </button>
        </div>

        {/* Manual Remap Dropdown */}
        {showManualRemap ? (
          <div className="p-3 bg-slate-50 rounded-lg border border-slate-200 mb-2 space-y-2">
            <label className="text-[11px] font-semibold text-slate-700 block">
              Select Schedule Activity:
            </label>
            <div className="flex gap-2">
              <select
                value={manualTaskId}
                onChange={(e) => setManualTaskId(e.target.value)}
                className="flex-1 bg-white border border-slate-200 rounded-lg px-2.5 py-1.5 text-[12px] text-slate-700 focus:ring-1 focus:ring-teal-500"
              >
                <option value="">-- Choose schedule task --</option>
                {scheduleTasks.map((t) => (
                  <option key={t.task_id} value={t.task_id}>
                    {t.task_id} - {t.task_name}
                  </option>
                ))}
              </select>
              <button
                onClick={handleManualRemapApply}
                disabled={!manualTaskId || isProcessing}
                className="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-[11px] font-bold transition disabled:opacity-50 cursor-pointer"
              >
                Select
              </button>
            </div>
          </div>
        ) : (
          /* Radio Match Options */
          <div className="space-y-2">
            {/* Primary Suggested Option */}
            {suggested && (
              <label
                onClick={() => setSelectedTaskId(suggested.source_task_id || suggested.task_id)}
                className={`flex items-center justify-between p-2.5 rounded-lg border cursor-pointer transition ${
                  selectedTaskId === (suggested.source_task_id || suggested.task_id)
                    ? 'border-teal-500 bg-teal-50/40 shadow-xs'
                    : 'border-slate-200 bg-white hover:bg-slate-50'
                }`}
              >
                <div className="flex items-center gap-2.5">
                  <input
                    type="radio"
                    name="match_selection"
                    value={suggested.source_task_id || suggested.task_id}
                    checked={selectedTaskId === (suggested.source_task_id || suggested.task_id)}
                    onChange={() => setSelectedTaskId(suggested.source_task_id || suggested.task_id)}
                    className="w-4 h-4 text-teal-600 focus:ring-teal-500 border-slate-300"
                  />
                  <div>
                    <div className="text-[12px] font-bold text-slate-900">
                      {suggested.source_task_id || suggested.task_id} {suggested.activity || suggested.task_name}
                    </div>
                    <div className="text-[10.5px] text-slate-500">
                      {formatDateRange(suggested.planned_start, suggested.planned_end)}
                    </div>
                  </div>
                </div>
                <span className="px-2 py-0.5 rounded text-[11px] font-bold text-teal-800 bg-teal-100">
                  {confScore > 0 ? `${confScore}% Match` : 'Suggested'}{' '}
                  <span className="font-normal text-[10px]">(AI Recommended)</span>
                </span>
              </label>
            )}

            {/* Alternative Candidate Tasks */}
            {alternativeCandidates.slice(0, 2).map((cand) => {
              const candId = cand.source_task_id || cand.task_id;
              const isSelected = selectedTaskId === candId;

              return (
                <label
                  key={candId}
                  onClick={() => setSelectedTaskId(candId)}
                  className={`flex items-center justify-between p-2.5 rounded-lg border cursor-pointer transition ${
                    isSelected
                      ? 'border-teal-500 bg-teal-50/40 shadow-xs'
                      : 'border-slate-200 bg-white hover:bg-slate-50'
                  }`}
                >
                  <div className="flex items-center gap-2.5">
                    <input
                      type="radio"
                      name="match_selection"
                      value={candId}
                      checked={isSelected}
                      onChange={() => setSelectedTaskId(candId)}
                      className="w-4 h-4 text-teal-600 focus:ring-teal-500 border-slate-300"
                    />
                    <div>
                      <div className="text-[12px] font-semibold text-slate-800">
                        {candId} {cand.activity || cand.task_name}
                      </div>
                      <div className="text-[10.5px] text-slate-500">
                        {formatDateRange(cand.planned_start, cand.planned_end)}
                      </div>
                    </div>
                  </div>
                  <span className="text-[11px] font-semibold text-slate-500">
                    Alternative Candidate
                  </span>
                </label>
              );
            })}

            {/* If neither suggested nor candidates exist */}
            {!suggested && alternativeCandidates.length === 0 && (
              <div className="p-3 bg-slate-50 rounded-lg text-slate-500 text-[11.5px] text-center border border-slate-200">
                No automatic task suggestions found. Click "Choose Other Task" above to select an activity.
              </div>
            )}
          </div>
        )}
      </div>

      {/* Action Buttons */}
      <div className="grid grid-cols-2 gap-2.5 pt-3 mt-2 border-t border-slate-100">
        <button
          onClick={handleApprove}
          disabled={isProcessing || !selectedTaskId}
          className="w-full py-2 bg-teal-700 hover:bg-teal-800 text-white rounded-lg font-bold text-[12px] flex items-center justify-center gap-1.5 shadow-sm transition disabled:opacity-50 cursor-pointer"
        >
          {isProcessing ? (
            <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
          ) : (
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path d="M5 13l4 4L19 7" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" />
            </svg>
          )}
          <span>{isProcessing ? 'Processing...' : 'Approve Match'}</span>
        </button>

        <button
          onClick={handleReject}
          disabled={isProcessing}
          className="w-full py-2 bg-blue-100/70 hover:bg-blue-200/70 text-blue-900 rounded-lg font-bold text-[12px] flex items-center justify-center gap-1.5 transition disabled:opacity-50 cursor-pointer"
        >
          {isProcessing ? (
            <div className="w-4 h-4 border-2 border-blue-900 border-t-transparent rounded-full animate-spin" />
          ) : (
            <svg className="w-4 h-4 text-blue-700" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path d="M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
            </svg>
          )}
          <span>Reject / Unmatch</span>
        </button>
      </div>
    </div>
  );
}


