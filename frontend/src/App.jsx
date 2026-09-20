import React, { useState } from 'react';
import useDashboardData from './hooks/useDashboardData';
import Sidebar from './components/Sidebar';
import TopNavbar from './components/TopNavbar';
import HeaderRow from './components/HeaderRow';
import MetricCardsGrid from './components/MetricCardsGrid';
import CriticalAlertBanner from './components/CriticalAlertBanner';
import ProjectScheduleTable from './components/ProjectScheduleTable';
import GanttTimeline from './components/GanttTimeline';
import RecentSiteUpdatesTable from './components/RecentSiteUpdatesTable';
import PlannerReviewQueueCard from './components/PlannerReviewQueueCard';
import ImpactChainModal from './components/ImpactChainModal';
import IngestModal from './components/IngestModal';

/**
 * Main ConstructLink Application assembling the Stitch UI and dynamic backend state.
 */
export default function App() {
  const {
    tasks,
    updates,
    reviewItems,
    deviationData,
    metrics,
    atRiskRange,
    atRiskDescription,
    loading,
    error,
    lastUpdated,
    refresh,
    handleApproveReview,
    handleRejectReview,
    handleChangeMatch,
  } = useDashboardData();

  // Navigation and UI state
  const [activeTab, setActiveTab] = useState('overview');
  const [searchQuery, setSearchQuery] = useState('');
  const [impactModalTaskId, setImpactModalTaskId] = useState(null);
  const [isIngestModalOpen, setIsIngestModalOpen] = useState(false);
  const [ingestModalTab, setIngestModalTab] = useState('text');
  const [ingestModalUploadMode, setIngestModalUploadMode] = useState('updates');
  const [isProcessingAction, setIsProcessingAction] = useState(false);
  const [toastMessage, setToastMessage] = useState(null);

  const showToast = (msg, type = 'success') => {
    setToastMessage({ msg, type });
    setTimeout(() => setToastMessage(null), 4000);
  };

  const handleOpenNewUpdate = () => {
    setIngestModalTab('text');
    setIngestModalUploadMode('updates');
    setIsIngestModalOpen(true);
  };

  const handleOpenUpload = () => {
    setIngestModalTab('file');
    setIngestModalUploadMode('updates');
    setIsIngestModalOpen(true);
  };

  const handleOpenImportSchedule = () => {
    setIngestModalTab('file');
    setIngestModalUploadMode('schedule');
    setIsIngestModalOpen(true);
  };

  const handleViewImpactChain = (taskId) => {
    setImpactModalTaskId(taskId || metrics.criticalDeviation?.task_id || 'T102');
  };

  const handleTabSelect = (tabId) => {
    setActiveTab(tabId);
    if (tabId === 'ingestion') {
      handleOpenNewUpdate();
    } else if (tabId === 'risk') {
      handleViewImpactChain(metrics.criticalDeviation?.task_id || 'T102');
    } else if (tabId === 'review') {
      const el = document.getElementById('planner-review-section');
      el?.scrollIntoView({ behavior: 'smooth' });
    } else if (tabId === 'schedule') {
      const el = document.getElementById('project-schedule-section');
      el?.scrollIntoView({ behavior: 'smooth' });
    }
  };

  // Wrap review actions with loading and user feedback
  const onApprove = async (id, targetTaskId) => {
    setIsProcessingAction(true);
    try {
      await handleApproveReview(id, targetTaskId);
      showToast(`Review item match approved. Schedule updated.`);
    } catch (err) {
      showToast(err.message || 'Failed to approve match.', 'error');
    } finally {
      setIsProcessingAction(false);
    }
  };

  const onReject = async (id) => {
    setIsProcessingAction(true);
    try {
      await handleRejectReview(id);
      showToast(`Review item marked as intentionally unmatched.`);
    } catch (err) {
      showToast(err.message || 'Failed to reject match.', 'error');
    } finally {
      setIsProcessingAction(false);
    }
  };

  const onChangeMatch = async (id, newTaskId) => {
    setIsProcessingAction(true);
    try {
      await handleChangeMatch(id, newTaskId);
      showToast(`Review item remapped to ${newTaskId}. Schedule updated.`);
    } catch (err) {
      showToast(err.message || 'Failed to remap match.', 'error');
    } finally {
      setIsProcessingAction(false);
    }
  };

  return (
    <div className="min-h-screen flex bg-[#eef3f8] antialiased text-slate-800 text-[13px]">
      {/* Toast Notification Banner */}
      {toastMessage && (
        <div className="fixed bottom-5 right-5 z-50 animate-in slide-in-from-bottom-3 fade-in duration-300">
          <div
            className={`px-4 py-3 rounded-xl shadow-lg border text-[12.5px] font-bold flex items-center gap-2.5 ${
              toastMessage.type === 'error'
                ? 'bg-rose-900 text-white border-rose-700'
                : 'bg-[#091628] text-white border-cyan-500/30'
            }`}
          >
            <span className={toastMessage.type === 'error' ? 'text-rose-400' : 'text-cyan-400'}>
              {toastMessage.type === 'error' ? '✕' : '✓'}
            </span>
            <span>{toastMessage.msg}</span>
          </div>
        </div>
      )}

      {/* Left Sidebar */}
      <Sidebar
        activeTab={activeTab}
        onSelectTab={handleTabSelect}
        pendingReviewCount={reviewItems.length}
      />

      {/* Main Content Area */}
      <div className="flex-1 ml-64 flex flex-col min-w-0">
        {/* Top Navbar */}
        <TopNavbar
          searchQuery={searchQuery}
          onSearchChange={setSearchQuery}
          notificationCount={reviewItems.length + (metrics.criticalDeviation ? 1 : 0)}
        />

        {/* Dashboard Body Content */}
        <main className="flex-1 px-8 pb-10 space-y-5">
          {/* Header Row */}
          <HeaderRow
            location={tasks[0]?.location || 'Project Site'}
            lastUpdated={lastUpdated}
            isRefreshing={loading}
            onRefresh={refresh}
            onOpenNewUpdate={handleOpenNewUpdate}
            onOpenUpload={handleOpenUpload}
          />

          {/* Backend Connection Warning if error exists */}
          {error && (
            <div className="p-3 bg-amber-50 border border-amber-200 rounded-xl text-[12px] text-amber-900 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="font-bold">⚠️ Backend Notice:</span>
                <span>{error} (Rendering available offline/cached schedule baseline)</span>
              </div>
              <button
                onClick={refresh}
                className="font-bold text-amber-900 underline hover:text-amber-950 cursor-pointer text-[11px]"
              >
                Retry Connection
              </button>
            </div>
          )}

          {/* 4 Metric Cards Grid */}
          <MetricCardsGrid
            overallProgress={metrics.overallProgress}
            criticalDeviation={metrics.criticalDeviation}
            statusCounts={metrics.statusCounts}
            aiMetrics={metrics.aiMetrics}
            isLoading={loading}
          />

          {/* Critical Alert Banner */}
          <CriticalAlertBanner
            criticalDeviation={metrics.criticalDeviation}
            atRiskCount={metrics.statusCounts.at_risk}
            atRiskRange={atRiskRange}
            atRiskDescription={atRiskDescription}
            tasksCount={tasks.length}
            isLoading={loading}
            onViewImpactChain={handleViewImpactChain}
          />

          {/* Middle Split Section: Schedule Table & Gantt Timeline */}
          <section id="project-schedule-section" className="grid grid-cols-12 gap-4">
            <div className="col-span-7">
              <ProjectScheduleTable
                tasks={tasks}
                isLoading={loading}
                error={error}
                onSelectTask={handleViewImpactChain}
                searchQuery={searchQuery}
                onOpenImportSchedule={handleOpenImportSchedule}
              />
            </div>
            <div className="col-span-5">
              <GanttTimeline
                tasks={tasks}
                isLoading={loading}
                onSelectTask={handleViewImpactChain}
              />
            </div>
          </section>

          {/* Bottom Split Section: Recent Site Updates & Planner Review Queue */}
          <section id="planner-review-section" className="grid grid-cols-12 gap-4">
            <div className="col-span-7">
              <RecentSiteUpdatesTable
                updates={updates}
                isLoading={loading}
                error={error}
                onOpenNewUpdate={handleOpenNewUpdate}
                onSelectTask={handleViewImpactChain}
              />
            </div>
            <div className="col-span-5">
              <PlannerReviewQueueCard
                items={reviewItems}
                scheduleTasks={tasks}
                isLoading={loading}
                error={error}
                onApprove={onApprove}
                onReject={onReject}
                onChangeMatch={onChangeMatch}
                isProcessing={isProcessingAction}
              />
            </div>
          </section>
        </main>
      </div>

      {/* Downstream Impact Chain Modal */}
      <ImpactChainModal
        isOpen={Boolean(impactModalTaskId)}
        onClose={() => setImpactModalTaskId(null)}
        taskId={impactModalTaskId}
        allTasks={tasks}
      />

      {/* Site Update & Spreadsheet Ingestion Modal */}
      <IngestModal
        isOpen={isIngestModalOpen}
        onClose={() => setIsIngestModalOpen(false)}
        initialTab={ingestModalTab}
        initialUploadMode={ingestModalUploadMode}
        onSuccess={() => {
          refresh();
        }}
      />
    </div>
  );
}

