import { useState, useEffect, useCallback } from 'react';
import {
  getScheduleDeviation,
  getLinkedTasks,
  getReviewQueue,
  getSiteUpdates,
  getSiteUpdateResult,
  approveReviewItem,
  changeReviewItemMatch,
  rejectReviewItem,
  resolveReviewItem,
  createSiteUpdate,
  processSiteUpdate,
  importSpreadsheet,
  importSchedule,
} from '../api/client';

/**
 * Calculates the planned percentage for a task given a reference calendar date.
 * Based purely on the task's planned_start and planned_end.
 */
function calculatePlannedPercent(task, refDateStr) {
  if (!task.planned_start || !task.planned_end || !refDateStr) return 0;
  if (refDateStr >= task.planned_end) return 100;
  if (refDateStr <= task.planned_start) return 0;

  const start = new Date(task.planned_start).getTime();
  const end = new Date(task.planned_end).getTime();
  const ref = new Date(refDateStr).getTime();

  if (isNaN(start) || isNaN(end) || isNaN(ref) || end <= start) return 100;
  const elapsed = ref - start;
  const total = end - start;
  return Math.min(100, Math.max(0, Math.round((elapsed / total) * 100)));
}

export function useDashboardData() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [deviationData, setDeviationData] = useState({ summary: null, tasks: [] });
  const [linkedTasks, setLinkedTasks] = useState([]);
  const [pendingReviews, setPendingReviews] = useState([]);
  const [allReviews, setAllReviews] = useState([]);
  const [siteUpdates, setSiteUpdates] = useState([]);
  const [lastUpdated, setLastUpdated] = useState(new Date());

  const fetchData = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);

      const [devRes, linkedRes, pendingReviewsRes, allReviewsRes, updatesRes] = await Promise.allSettled([
        getScheduleDeviation(),
        getLinkedTasks(),
        getReviewQueue('pending'),
        getReviewQueue('all'),
        getSiteUpdates(),
      ]);

      if (devRes.status === 'fulfilled') {
        setDeviationData(devRes.value || { summary: null, tasks: [] });
      } else {
        console.warn('Failed to load schedule deviation:', devRes.reason);
        throw new Error(devRes.reason?.message || 'Failed to load schedule deviation from backend');
      }

      if (linkedRes.status === 'fulfilled') {
        setLinkedTasks(linkedRes.value || []);
      }

      if (pendingReviewsRes.status === 'fulfilled') {
        setPendingReviews(pendingReviewsRes.value || []);
      }

      if (allReviewsRes.status === 'fulfilled') {
        setAllReviews(allReviewsRes.value || []);
      }

      if (updatesRes.status === 'fulfilled') {
        const rawUpdates = updatesRes.value || [];
        const devTasks = devRes.status === 'fulfilled' ? (devRes.value?.tasks || []) : [];
        const taskMap = new Map();
        devTasks.forEach((t) => {
          if (t.task_id) taskMap.set(String(t.task_id), t);
          if (t.source_task_id) taskMap.set(String(t.source_task_id), t);
        });

        // Correlate with review queue items first
        const reviewMap = new Map();
        const allReviewsList = allReviewsRes.status === 'fulfilled' ? (allReviewsRes.value || []) : [];
        allReviewsList.forEach((r) => {
          if (r.site_update_id) reviewMap.set(String(r.site_update_id), r);
        });

        const enrichPromises = rawUpdates.map(async (u) => {
          const rev = reviewMap.get(String(u.id));
          if (rev) {
            const rawMatch = rev.current_matched_task?.source_task_id ||
              rev.suggested_match?.source_task_id ||
              (rev.matched_task_id && taskMap.get(String(rev.matched_task_id))?.source_task_id) ||
              rev.matched_task_id;

            return {
              ...u,
              matched_task_id: rawMatch || null,
              matched_task_name: rev.current_matched_task?.activity || rev.suggested_match?.activity || rev.extracted_activity || null,
              progress_percent: rev.progress_percent !== null && rev.progress_percent !== undefined ? Number(rev.progress_percent) : null,
              status: rev.status || null,
              confidence_score: rev.confidence_score !== null && rev.confidence_score !== undefined ? Number(rev.confidence_score) : null,
              delay_days: rev.delay_days || null,
              delay_reason: rev.delay_reason || null,
              review_id: rev.review_id,
              review_reason: rev.review_reason,
              review_status: rev.review_status,
              is_processed: true,
            };
          }

          try {
            const proc = await getSiteUpdateResult(u.id);
            const matchedT = proc?.matched_task_id
              ? (taskMap.get(String(proc.matched_task_id))?.source_task_id || proc.matched_task_id)
              : null;
            const matchedName = proc?.matched_task_id
              ? (taskMap.get(String(proc.matched_task_id))?.activity || null)
              : null;

            return {
              ...u,
              matched_task_id: matchedT,
              matched_task_name: matchedName,
              progress_percent: proc?.progress_percent !== null && proc?.progress_percent !== undefined ? Number(proc.progress_percent) : null,
              status: proc?.status || null,
              confidence_score: proc?.confidence_score !== null && proc?.confidence_score !== undefined ? Number(proc.confidence_score) : null,
              delay_days: proc?.delay_days || null,
              delay_reason: proc?.delay_reason || null,
              is_processed: true,
            };
          } catch {
            return {
              ...u,
              matched_task_id: null,
              matched_task_name: null,
              progress_percent: null,
              status: null,
              confidence_score: null,
              delay_days: null,
              delay_reason: null,
              is_processed: false,
            };
          }
        });

        const enrichedUpdates = await Promise.all(enrichPromises);
        setSiteUpdates(enrichedUpdates);
      }

      setLastUpdated(new Date());
    } catch (err) {
      console.error('Error in useDashboardData:', err);
      setError(err.message || 'Failed to load dashboard data');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // Standardize raw tasks from deviationData
  const rawTasks = deviationData.tasks || [];
  const tasks = rawTasks.map((t) => {
    const rawProgress = t.progress_percent ?? t.actual_progress;
    const numProg = rawProgress !== null && rawProgress !== undefined ? Number(rawProgress) : null;
    const isCompleted = (t.status || '').toLowerCase() === 'completed' || (numProg !== null && numProg >= 100);
    const rawStatus = (t.status || '').toLowerCase().trim();
    let execStatus = 'planned';
    if (isCompleted) {
      execStatus = 'completed';
    } else if (rawStatus === 'delayed') {
      execStatus = 'delayed';
    } else if (rawStatus === 'in_progress' || (numProg !== null && numProg > 0)) {
      execStatus = 'in_progress';
    } else if (rawStatus && rawStatus !== 'not_started') {
      execStatus = rawStatus;
    }

    return {
      ...t,
      task_id: t.source_task_id || t.task_id,
      db_id: t.task_id,
      task_name: t.activity || t.task_name,
      planned_start: t.planned_start,
      planned_end: t.planned_end,
      actual_start: t.actual_start_date,
      actual_end: t.actual_end_date,
      progress_percent: numProg !== null ? numProg : 0,
      has_actual_progress: rawProgress !== null && rawProgress !== undefined,
      status: execStatus,
      delay_days: t.deviation?.effective_delay_days ?? t.delay_days ?? null,
      delay_reason: t.delay_reason || null,
      schedule_health: t.schedule_health || null,
      confidence_score: t.confidence_score !== null && t.confidence_score !== undefined ? Number(t.confidence_score) : null,
    };
  });

  // Summary from backend
  const summary = deviationData.summary || {
    total_tasks: tasks.length,
    on_time: 0,
    delayed: 0,
    at_risk: 0,
    unassessed: 0,
  };

  // Determine reference project date from site updates or today
  const reportedDates = siteUpdates.map((u) => u.reported_on).filter(Boolean);
  const refDateStr = reportedDates.length > 0
    ? [...reportedDates].sort().reverse()[0]
    : '2026-09-09';

  // 1. Overall Actual Progress (%) strictly from tasks in database
  const actualProgress = tasks.length > 0
    ? Math.round(tasks.reduce((sum, t) => sum + (t.progress_percent || 0), 0) / tasks.length)
    : 0;

  // 1b. Overall Planned Progress (%) dynamically calculated from task date intervals
  const plannedProgress = tasks.length > 0
    ? Math.round(tasks.reduce((sum, t) => sum + calculatePlannedPercent(t, refDateStr), 0) / tasks.length)
    : 0;

  // 2. Schedule Variance & Delayed Task
  const delayedTasks = tasks.filter(
    (t) =>
      (t.status || '').toLowerCase() !== 'completed' &&
      (t.progress_percent ?? 0) < 100 &&
      (t.schedule_health === 'delayed' ||
       (t.delay_days && t.delay_days > 0) ||
       (t.deviation?.effective_delay_days && t.deviation.effective_delay_days > 0) ||
       ((t.status || '').toLowerCase().includes('delay') && ((t.delay_days && t.delay_days > 0) || (t.deviation?.effective_delay_days && t.deviation.effective_delay_days > 0))))
  );

  const criticalDelayedTask = delayedTasks.length > 0
    ? delayedTasks.reduce((max, t) => {
        const tSlip = t.delay_days ?? t.deviation?.effective_delay_days ?? t.deviation?.slip_days ?? 0;
        const maxSlip = max.delay_days ?? max.deviation?.effective_delay_days ?? max.deviation?.slip_days ?? 0;
        return tSlip >= maxSlip ? t : max;
      }, delayedTasks[0])
    : null;

  const criticalDeviation = criticalDelayedTask
    ? {
        task_id: criticalDelayedTask.source_task_id || criticalDelayedTask.task_id,
        task_name: criticalDelayedTask.activity || criticalDelayedTask.task_name,
        slipDays: criticalDelayedTask.delay_days ?? criticalDelayedTask.deviation?.effective_delay_days ?? criticalDelayedTask.deviation?.slip_days ?? 0,
        reason: criticalDelayedTask.delay_reason || 'critical path weather and operational disruption',
        progress_percent: Math.round(criticalDelayedTask.progress_percent || 0),
      }
    : null;

  // Downstream tasks at risk
  const atRiskTasks = tasks.filter((t) => t.schedule_health === 'at_risk');

  // Compute at-risk range and description dynamically
  let atRiskRange = '';
  let atRiskDescription = '';
  if (atRiskTasks.length > 0) {
    const sortedAtRisk = [...atRiskTasks].sort((a, b) =>
      (a.planned_start || '').localeCompare(b.planned_start || '')
    );
    const firstTask = sortedAtRisk[0];
    const lastTask = sortedAtRisk[sortedAtRisk.length - 1];
    atRiskRange = firstTask.task_id === lastTask.task_id
      ? firstTask.task_id
      : `${firstTask.task_id}–${lastTask.task_id}`;
    atRiskDescription = `Downstream activities ${firstTask.task_id} ${firstTask.task_name} through ${lastTask.task_id} ${lastTask.task_name} are at risk unless mitigation is taken.`;
  }

  // 3. Schedule Status Breakdown
  const statusCounts = {
    total: summary.total_tasks || tasks.length,
    completed: tasks.filter((t) => (t.status || '').toLowerCase() === 'completed' || t.progress_percent === 100).length,
    in_progress: tasks.filter(
      (t) =>
        ((t.status || '').toLowerCase() === 'in_progress' || (t.progress_percent > 0 && t.progress_percent < 100)) &&
        t.schedule_health !== 'delayed'
    ).length,
    delayed: summary.delayed ?? delayedTasks.length,
    at_risk: summary.at_risk ?? atRiskTasks.length,
    planned: tasks.filter(
      (t) =>
        (!t.status || t.status === 'planned') &&
        !t.progress_percent &&
        t.schedule_health !== 'at_risk' &&
        t.schedule_health !== 'delayed'
    ).length,
  };

  // 4. AI Extraction & Linking Metrics from real backend scores
  const confidenceScores = [];
  linkedTasks.forEach((t) => {
    if (t.confidence_score !== null && t.confidence_score !== undefined) {
      const val = Number(t.confidence_score);
      confidenceScores.push(val <= 1.0 ? val * 100 : val);
    }
  });
  allReviews.forEach((r) => {
    if (r.confidence_score !== null && r.confidence_score !== undefined) {
      const val = Number(r.confidence_score);
      if (val > 0) {
        confidenceScores.push(val <= 1.0 ? val * 100 : val);
      }
    }
  });

  const avgConfidence = confidenceScores.length > 0
    ? Math.round(confidenceScores.reduce((a, b) => a + b, 0) / confidenceScores.length)
    : 0;

  const totalLinked = linkedTasks.filter(
    (t) => t.confidence_score !== null && t.confidence_score !== undefined
  ).length;

  const aiMetrics = {
    avgConfidence,
    totalLinked,
    totalTasks: tasks.length,
    totalExtracted: siteUpdates.length,
    hasExtractions: confidenceScores.length > 0,
  };

  const metrics = {
    overallProgress: {
      actual: actualProgress,
      planned: plannedProgress,
    },
    criticalDeviation,
    statusCounts,
    aiMetrics,
  };

  // Action Handlers
  const handleApprove = async (reviewId, notes) => {
    await approveReviewItem(reviewId, notes);
    await fetchData();
  };

  const handleChangeMatch = async (reviewId, taskId, notes) => {
    await changeReviewItemMatch(reviewId, taskId, notes);
    await fetchData();
  };

  const handleReject = async (reviewId, reason) => {
    await rejectReviewItem(reviewId, reason);
    await fetchData();
  };

  const handleResolve = async (reviewId, action, taskId, notes) => {
    await resolveReviewItem(reviewId, action, taskId, notes);
    await fetchData();
  };

  const handleCreateUpdate = async ({ raw_update, location, reported_on, source_reference }) => {
    const created = await createSiteUpdate({
      raw_update,
      location: location || 'Well Pad A',
      reported_on: reported_on || '09-Sep-2026',
      source_reference: source_reference || 'Manual Input',
    });
    if (created && created.source_update_id) {
      try {
        await processSiteUpdate(created.source_update_id);
      } catch (err) {
        console.warn('AI processing error on new update:', err);
      }
    }
    await fetchData();
  };

  const handleUploadSpreadsheet = async (file) => {
    const result = await importSpreadsheet(file);
    await fetchData();
    return result;
  };

  const handleUploadSchedule = async (file) => {
    const result = await importSchedule(file);
    await fetchData();
    return result;
  };

  return {
    loading,
    error,
    lastUpdated,
    refresh: fetchData,
    tasks,
    updates: siteUpdates,
    reviewItems: pendingReviews,
    deviationData,
    metrics,
    atRiskTasks,
    atRiskRange,
    atRiskDescription,
    handleApproveReview: handleApprove,
    handleChangeMatch,
    handleRejectReview: handleReject,
    handleResolveReview: handleResolve,
    handleCreateUpdate,
    handleUploadSpreadsheet,
    handleUploadSchedule,
  };
}

export default useDashboardData;
