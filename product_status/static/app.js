const state = {
  summitLabel: "",
  squadsByKey: new Map(),
  collapsed: new Set(),
  // Sprint data is always omitted (both here and in `publishToNotion`) -
  // there's no checkbox for it anymore, see `renderSprintDataHiddenBlock`.
  showSprintData: false,
  // Mirrors the "Only Star Projects" checkbox in the EPD Report toolbar -
  // the web view renders exactly what would be published to Notion given
  // its current state (see `renderAll`), rather than it only affecting the
  // Notion export.
  onlyStarProjects: false,
  // Only affects the Notion export (see `publishToNotion`) - the web view
  // always shows every squad regardless of this checkbox.
  demoRun: false,
  // Which sprint sub-tab ("current"/"previous") is showing for each team in
  // the Sprint Report tab, keyed by team key. Missing entries default to
  // "current" - see `renderSprintReportTeam`.
  sprintReportSubTab: new Map(),
  // Fallback target Notion page (URL) for the "Publishes to" bars, used
  // whenever a tab has no override in localStorage - filled in from
  // `/api/notion/status` (see `loadNotionStatus`/`notion_report.DEFAULT_PARENT_PAGE_URL`).
  notionDefaultParentPageUrl: "",
};

// Canonical project lifecycle milestones - the milestones table only shows
// milestones that fuzzy-match one of these (in this order); anything else
// is left out entirely so the card stays scannable. Kept in sync by hand
// with `product_status/milestones.py:KEY_MILESTONE_NAMES` (this runs
// client-side and can't import that module directly).
const KEY_MILESTONE_NAMES = ["Product: Define", "Design: Shape", "Design: Refine", "Early Access", "Public Launch"];

// Loose match: lowercase and strip everything but letters/digits, so
// differences in punctuation, spacing, and casing (e.g. "product define",
// "Product - Define", "PRODUCT: DEFINE") all still match "Product: Define".
function normalizeMilestoneName(name) {
  return (name || "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

function matchKeyMilestones(milestones) {
  const targets = KEY_MILESTONE_NAMES.map((name) => ({ name, norm: normalizeMilestoneName(name) }));
  const byTarget = new Map();
  for (const milestone of milestones) {
    const norm = normalizeMilestoneName(milestone.name);
    const target = targets.find((t) => norm === t.norm || norm.includes(t.norm) || t.norm.includes(norm));
    if (target && !byTarget.has(target.name)) {
      byTarget.set(target.name, milestone);
    }
  }
  return KEY_MILESTONE_NAMES.filter((name) => byTarget.has(name)).map((name) => byTarget.get(name));
}

const els = {
  errorBanner: document.getElementById("error-banner"),
  successBanner: document.getElementById("success-banner"),
  notionStatus: document.getElementById("notion-status"),
  notionDisconnectBtn: document.getElementById("notion-disconnect-btn"),
  notionConnectLink: document.getElementById("notion-connect-link"),
  notionBtn: document.getElementById("notion-btn"),
  onlyStarProjectsCheckbox: document.getElementById("only-star-projects-checkbox"),
  demoRunCheckbox: document.getElementById("demo-run-checkbox"),
  squadsContainer: document.getElementById("squads-container"),
  loadingState: document.getElementById("loading-state"),
  tabButtons: document.querySelectorAll(".tab-btn"),
  tabPanels: document.querySelectorAll(".tab-panel"),
  sprintReportContainer: document.getElementById("sprint-report-container"),
  sprintReportNotionBtn: document.getElementById("sprint-report-notion-btn"),
  notionTargetBars: document.querySelectorAll(".notion-target-bar"),
  topbarUser: document.getElementById("topbar-user"),
  topbarUserAvatar: document.getElementById("topbar-user-avatar"),
  topbarUserName: document.getElementById("topbar-user-name"),
  milestonesReportContainer: document.getElementById("milestones-report-container"),
  milestonesUpdateBtn: document.getElementById("milestones-update-btn"),
  milestonesQuarterLabel: document.getElementById("milestones-quarter-label"),
  milestonesUpdatedAt: document.getElementById("milestones-updated-at"),
  supportReportContainer: document.getElementById("support-report-container"),
  supportReportUpdateBtn: document.getElementById("support-report-update-btn"),
  supportReportUpdatedAt: document.getElementById("support-report-updated-at"),
  partnerInsightsTabBtn: document.getElementById("partner-insights-tab-btn"),
  partnerInsightsContainer: document.getElementById("partner-insights-container"),
  partnerInsightsUpdateBtn: document.getElementById("partner-insights-update-btn"),
  partnerInsightsUpdatedAt: document.getElementById("partner-insights-updated-at"),
};

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatDate(isoOrTimelessDate) {
  if (!isoOrTimelessDate) return "—";

  // Linear's `TimelessDate` scalar (project/milestone dates, e.g.
  // "2026-08-14") has no time or timezone component at all - it's just a
  // calendar date. Passing a bare "YYYY-MM-DD" string to `new Date()` parses
  // it as UTC midnight, which then shifts a day earlier once converted to
  // any timezone behind UTC. Build the Date from its parts instead so it's
  // anchored to local midnight and always displays the intended day.
  const dateOnlyMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoOrTimelessDate);
  const d = dateOnlyMatch
    ? new Date(Number(dateOnlyMatch[1]), Number(dateOnlyMatch[2]) - 1, Number(dateOnlyMatch[3]))
    : new Date(isoOrTimelessDate);

  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function formatRelativeTime(epochSeconds) {
  const diffMs = Date.now() - epochSeconds * 1000;
  const diffMin = Math.round(diffMs / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin} minute${diffMin === 1 ? "" : "s"} ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr} hour${diffHr === 1 ? "" : "s"} ago`;
  const diffDay = Math.round(diffHr / 24);
  return `${diffDay} day${diffDay === 1 ? "" : "s"} ago`;
}

function isStale(fetchedAt) {
  return Date.now() / 1000 - fetchedAt > 24 * 60 * 60;
}

// "(<n> days ago)"-style text for a project update's `createdAt` - mirrors
// `notion_report.py:_relative_days_ago`. Day-granularity (not hours/minutes)
// to match how the Notion export reads.
function formatRelativeDays(isoString) {
  if (!isoString) return "";
  const created = new Date(isoString);
  if (Number.isNaN(created.getTime())) return "";
  const diffDays = Math.floor((Date.now() - created.getTime()) / (24 * 60 * 60 * 1000));
  if (diffDays <= 0) return "today";
  if (diffDays === 1) return "1 day ago";
  return `${diffDays} days ago`;
}

// Keeps each squad header docked directly below the (sticky) topbar - see
// `.squad-header`'s `top: var(--topbar-h)` in style.css. Measured rather
// than hardcoded so it stays correct across browsers/font rendering and if
// the topbar/tabbar's contents ever change height. `--topbar-h` positions
// the tabbar right below the topbar; `--sticky-offset` (topbar + tabbar)
// positions each squad-header right below both.
function syncTopbarHeight() {
  const topbar = document.querySelector(".topbar");
  const tabbar = document.querySelector(".tabbar");
  if (!topbar) return;
  const topbarHeight = topbar.offsetHeight;
  document.documentElement.style.setProperty("--topbar-h", `${topbarHeight}px`);
  document.documentElement.style.setProperty(
    "--sticky-offset",
    `${topbarHeight + (tabbar ? tabbar.offsetHeight : 0)}px`
  );
}

function cycleDisplayName(cycle) {
  return cycle.name || `Cycle ${cycle.number}`;
}

function statusBadgeClass(statusType) {
  const known = ["backlog", "planned", "started", "completed", "canceled", "paused"];
  return known.includes(statusType) ? `status-${statusType}` : "status-backlog";
}

function healthBadgeClass(health) {
  const map = { onTrack: "status-completed", atRisk: "status-planned", offTrack: "status-canceled" };
  return map[health] || "status-backlog";
}

// ---- Projects ----

function renderMilestone(milestone) {
  const checkClass = milestone.completed
    ? "done"
    : milestone.status === "overdue"
    ? "overdue"
    : "";
  return `
    <div class="milestone-row">
      <span class="milestone-check ${checkClass}">${milestone.completed ? "✓" : ""}</span>
      <span class="milestone-name ${milestone.completed ? "done" : ""}">${escapeHtml(
    milestone.name
  )}</span>
      <span class="milestone-date">${formatDate(milestone.targetDate)}</span>
    </div>`;
}

function renderMilestonesSection(project) {
  if (!project.milestones.length) {
    return '<p class="empty-note">No milestones defined.</p>';
  }

  const matched = matchKeyMilestones(project.milestones);
  if (!matched.length) {
    return '<p class="empty-note">None of the tracked milestones are defined for this project.</p>';
  }

  return matched.map(renderMilestone).join("");
}

function renderLastUpdate(lastUpdate) {
  if (!lastUpdate) {
    return '<p class="empty-note">No project updates yet.</p>';
  }
  const bodyText = (lastUpdate.body || "").trim();
  // `.last-update-body` uses `white-space: pre-wrap`, so line breaks in the
  // escaped text render as-is without needing `<br>` tags.
  const bodyHtml = bodyText ? escapeHtml(bodyText) : '<span class="empty-note">No update content.</span>';
  const dateTitle = lastUpdate.createdAt ? new Date(lastUpdate.createdAt).toLocaleString() : "";
  const relative = formatRelativeDays(lastUpdate.createdAt);
  const dateText = relative ? `${formatDate(lastUpdate.createdAt)} (${relative})` : formatDate(lastUpdate.createdAt);

  return `
    <div class="last-update">
      <div class="last-update-meta">
        <span class="last-update-author">${escapeHtml(lastUpdate.author || "Unknown")}</span>
        <span class="status-badge ${healthBadgeClass(lastUpdate.health)}">${escapeHtml(
    lastUpdate.healthLabel || "—"
  )}</span>
        <span class="last-update-date" title="${escapeHtml(dateTitle)}">${escapeHtml(dateText)}</span>
      </div>
      <div class="last-update-body">${bodyHtml}</div>
    </div>`;
}

function renderProjectCard(project) {
  const progressPct = Math.round((project.progress || 0) * 100);
  const milestones = renderMilestonesSection(project);

  return `
    <div class="card">
      <div class="card-title">
        <a href="${escapeHtml(project.url)}" target="_blank" rel="noopener">${escapeHtml(
    project.name
  )}</a>
        <span class="status-badge ${statusBadgeClass(project.statusType)}">${escapeHtml(
    project.status
  )}</span>
      </div>
      <div class="project-meta-row">
        <span>${formatDate(project.startDate)} → ${formatDate(project.targetDate)}</span>
      </div>
      <div class="progress-bar-track">
        <div class="progress-bar-fill" style="width: ${progressPct}%"></div>
      </div>
      <div class="milestone-list">${milestones}</div>
      <div class="last-update-section">
        <h4 class="last-update-title">Last update</h4>
        ${renderLastUpdate(project.lastUpdate)}
      </div>
    </div>`;
}

function renderProjectGroupBody(projects, emptyNote) {
  return projects.length
    ? `<div class="squad-grid">${projects.map(renderProjectCard).join("")}</div>`
    : `<p class="empty-note">${escapeHtml(emptyNote)}</p>`;
}

function renderProjectGroup(title, badge, projects, emptyNote) {
  return `
    <div class="project-group">
      <h4 class="project-group-title">${escapeHtml(title)}${
    badge ? ` <span class="label-badge">${escapeHtml(badge)}</span>` : ""
  }</h4>
      ${renderProjectGroupBody(projects, emptyNote)}
    </div>`;
}

// Mirrors `notion_report.py:build_team_blocks` / `_project_content`: when
// `onlyStarProjects` is set, the block title itself states the label and
// only that group is shown (no separate subtitle needed); otherwise the
// title reverts to the generic "Projects", the Star Project group is shown
// under its own subtitle, and every other current-quarter project is shown
// under an "Other Projects" subtitle (collapsed into a toggle heading in
// the Notion export).
function renderProjectsBlock(squad, summitLabel, onlyStarProjects) {
  const summitProjects = squad.summitProjects || [];
  const otherProjects = squad.otherProjects || [];

  const title = onlyStarProjects ? `Projects with label "${summitLabel}"` : "Projects";
  const groups = onlyStarProjects
    ? renderProjectGroupBody(summitProjects, `No projects tagged "${summitLabel}" for this squad.`)
    : renderProjectGroup(
        "Star Project",
        `Projects with label "${summitLabel}"`,
        summitProjects,
        `No projects tagged "${summitLabel}" for this squad.`
      ) + renderProjectGroup("Other Projects", null, otherProjects, "No other projects for this squad.");

  return `
    <div class="squad-block">
      <h3 class="block-title">${escapeHtml(title)}</h3>
      ${groups}
    </div>`;
}

// ---- Quality ----

// Mirrors `notion_report.py:_QUALITY_DEFINITIONS` - shown above the table
// so readers don't have to guess what these two rows mean.
const QUALITY_DEFINITIONS = [
  ["Currently out of SLA", "Open bugs that have breached SLA."],
  ["Failed SLA this month", "Bugs that were fixed this month after they had breached their SLA"],
];

function renderQualityDefinitions() {
  const items = QUALITY_DEFINITIONS.map(
    ([label, text]) => `<li><strong>${escapeHtml(label)}:</strong> ${escapeHtml(text)}</li>`
  ).join("");
  return `<ul class="quality-definitions">${items}</ul>`;
}

function renderQualityBlock(quality) {
  if (!quality) {
    return `
      <div class="squad-block">
        <h3 class="block-title">Quality</h3>
        <p class="empty-note">No quality data available.</p>
      </div>`;
  }

  const rows = [
    {
      label: "SLA Quality Total",
      value: quality.slaQualityTotal,
      total: true,
      withinThreshold: quality.slaQualityWithinThreshold,
    },
    { label: "Currently Out of SLA", value: quality.currentlyOutOfSla },
    { label: "Failed SLA This Month", value: quality.failedSlaThisMonth },
    { label: "Currently Active High Bugs", value: quality.currentlyActiveHighBugs },
    // No data pull for this one - always blank, filled in by hand.
    { label: "Number of bugs because of missing tests", value: "" },
    {
      label: "Incoming Bugs with High or Urgent priority this month",
      value: quality.incomingHighUrgentThisMonth,
      total: true,
      withinThreshold: quality.incomingHighUrgentWithinThreshold,
    },
  ];

  const body = rows
    .map((row) => {
      const scored = row.withinThreshold !== undefined;
      const labelHtml = scored
        ? `${row.label} <span class="label-badge">limit ≤ ${quality.threshold}</span>`
        : row.label;
      const scoreClass = scored ? (row.withinThreshold ? " score-ok" : " score-over") : "";
      return `
        <tr${row.total ? ' class="total-row"' : ""}>
          <td>${labelHtml}</td>
          <td class="num${scoreClass}">${row.value}</td>
        </tr>`;
    })
    .join("");

  return `
    <div class="squad-block">
      <h3 class="block-title">Quality</h3>
      ${renderQualityDefinitions()}
      <table class="data-table">
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

// ---- Sprint tables ----

function currentSprintStatusSummary(sprint) {
  const statuses = sprint.statuses || [];
  const byAssignee = sprint.byAssignee || [];
  if (!statuses.length || !byAssignee.length) return "";
  const totals = Object.fromEntries(statuses.map((status) => [status, 0]));
  byAssignee.forEach((row) => {
    statuses.forEach((status) => {
      totals[status] += row.statusBreakdown[status] || 0;
    });
  });
  return statuses.map((status) => `<span>${escapeHtml(status)}: <strong>${totals[status]}</strong></span>`).join("");
}

function previousSprintStatusSummary(sprint) {
  const byAssignee = sprint.byAssignee || [];
  if (!byAssignee.length) return "";
  const totals = {
    Assigned: sprint.totalIssues || 0,
    Completed: byAssignee.reduce((sum, row) => sum + row.completed.count, 0),
    Canceled: byAssignee.reduce((sum, row) => sum + row.canceled.count, 0),
    "Moved to next": byAssignee.reduce((sum, row) => sum + row.movedToNextSprint.count, 0),
    Removed: byAssignee.reduce((sum, row) => sum + row.removedFromCycle.count, 0),
    "Added mid-cycle": byAssignee.reduce((sum, row) => sum + row.addedDuringCycle.count, 0),
  };
  return Object.entries(totals)
    .map(([label, value]) => `<span>${escapeHtml(label)}: <strong>${value}</strong></span>`)
    .join("");
}

function renderCurrentSprintBlock(sprint) {
  if (!sprint) {
    return `
      <div class="squad-block">
        <h3 class="block-title">Current sprint</h3>
        <p class="empty-note">No active cycle for this team.</p>
      </div>`;
  }

  const { cycle, totalIssues, byAssignee } = sprint;
  const statuses = sprint.statuses || [];

  const rows = byAssignee
    .map((row) => {
      const statusCells = statuses
        .map((status) => `<td class="num">${row.statusBreakdown[status] || 0}</td>`)
        .join("");
      return `
        <tr>
          <td>${escapeHtml(row.assignee)}</td>
          <td class="num">${row.total}</td>
          ${statusCells}
        </tr>`;
    })
    .join("");

  const statusHeaders = statuses
    .map((status) => `<th class="num">${escapeHtml(status)}</th>`)
    .join("");

  const table = byAssignee.length
    ? `
      <table class="data-table">
        <thead>
          <tr><th>Assignee</th><th class="num">Total</th>${statusHeaders}</tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`
    : '<p class="empty-note">No issues in this cycle.</p>';

  const summary = currentSprintStatusSummary(sprint);

  return `
    <div class="squad-block">
      <h3 class="block-title">Current sprint</h3>
      <div class="cycle-meta">
        <span class="cycle-name">${escapeHtml(cycleDisplayName(cycle))}</span>
        <span>${formatDate(cycle.startsAt)} → ${formatDate(cycle.endsAt)}</span>
        <span>· ${totalIssues} issue${totalIssues === 1 ? "" : "s"}</span>
      </div>
      ${summary ? `<div class="status-summary">${summary}</div>` : ""}
      ${table}
    </div>`;
}

function renderPreviousSprintBlock(sprint) {
  if (!sprint) {
    return `
      <div class="squad-block">
        <h3 class="block-title">Previous sprint</h3>
        <p class="empty-note">No completed cycle found for this team.</p>
      </div>`;
  }

  const { cycle, totalIssues, byAssignee } = sprint;
  const rows = byAssignee
    .map(
      (row) => `
        <tr>
          <td>${escapeHtml(row.assignee)}</td>
          <td class="num">${row.totalAssigned}</td>
          <td class="num">${row.completed.count}</td>
          <td class="num">${row.canceled.count}</td>
          <td class="num">${row.movedToNextSprint.count}</td>
          <td class="num">${row.removedFromCycle.count}</td>
          <td class="num">${row.addedDuringCycle.count}</td>
        </tr>`
    )
    .join("");

  const table = byAssignee.length
    ? `
      <table class="data-table">
        <thead>
          <tr>
            <th>Assignee</th>
            <th class="num">Assigned</th>
            <th class="num">Completed</th>
            <th class="num">Canceled</th>
            <th class="num">Moved to next</th>
            <th class="num">Removed</th>
            <th class="num">Added mid-cycle</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`
    : '<p class="empty-note">No issues were assigned.</p>';

  const summary = previousSprintStatusSummary(sprint);

  return `
    <div class="squad-block">
      <h3 class="block-title">Previous sprint</h3>
      <div class="cycle-meta">
        <span class="cycle-name">${escapeHtml(cycleDisplayName(cycle))}</span>
        <span>${formatDate(cycle.startsAt)} → ${formatDate(cycle.endsAt)}</span>
        <span>· ${totalIssues} issue${totalIssues === 1 ? "" : "s"} assigned</span>
      </div>
      ${summary ? `<div class="status-summary">${summary}</div>` : ""}
      ${table}
    </div>`;
}

// Mirrors `notion_report.py:build_team_blocks` when `skip_sprint_data` is
// set: Current Sprint keeps its heading but drops all stats/table, and
// Previous Sprint is omitted entirely (no heading either).
function renderSprintDataHiddenBlock() {
  return `
    <div class="squad-block">
      <h3 class="block-title">Current sprint</h3>
      <p class="empty-note">Sprint data hidden.</p>
    </div>`;
}

// ---- Sprint Report tab ----
// Reuses the same per-squad data already fetched for the EPD Report tab
// (`state.squadsByKey`) - one section per team, with "Current sprint" /
// "Previous sprint" sub-tabs so both are available without doubling the
// page length.

// Current sprint: the cycle's still in progress, so there's no "moved to
// next"/"removed" breakdown yet (see report.py:build_current_sprint) - just
// assigned/completed/added-mid-cycle.
function renderSprintReportAssigneeTable(byAssignee, emptyMessage) {
  if (!byAssignee.length) {
    return `<p class="empty-note">${escapeHtml(emptyMessage)}</p>`;
  }
  const rows = byAssignee
    .map(
      (row) => `
        <tr>
          <td>${escapeHtml(row.assignee)}</td>
          <td class="num">${row.total}</td>
          <td class="num">${row.completed.count}</td>
          <td class="num">${row.addedDuringCycle.count}</td>
        </tr>`
    )
    .join("");

  return `
    <table class="data-table">
      <thead>
        <tr>
          <th>Team member</th>
          <th class="num">Assigned</th>
          <th class="num">Completed</th>
          <th class="num">Added mid-cycle</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// Previous sprint: the cycle is closed, so every assigned ticket landed in
// exactly one of Completed/Canceled/Moved to next/Removed (see
// report.py:build_previous_sprint's docstring) - surfacing those
// separately (rather than folding them into "Assigned" with no further
// breakdown) is the whole point of this table, so it intentionally
// mirrors `renderPreviousSprintBlock`'s EPD Report table rather than
// reusing `renderSprintReportAssigneeTable` above.
function renderPreviousSprintReportAssigneeTable(byAssignee, emptyMessage) {
  if (!byAssignee.length) {
    return `<p class="empty-note">${escapeHtml(emptyMessage)}</p>`;
  }
  const rows = byAssignee
    .map(
      (row) => `
        <tr>
          <td>${escapeHtml(row.assignee)}</td>
          <td class="num">${row.totalAssigned}</td>
          <td class="num">${row.completed.count}</td>
          <td class="num">${row.canceled.count}</td>
          <td class="num">${row.movedToNextSprint.count}</td>
          <td class="num">${row.removedFromCycle.count}</td>
          <td class="num">${row.addedDuringCycle.count}</td>
        </tr>`
    )
    .join("");

  return `
    <table class="data-table">
      <thead>
        <tr>
          <th>Team member</th>
          <th class="num">Assigned</th>
          <th class="num">Completed</th>
          <th class="num">Canceled</th>
          <th class="num">Moved to next</th>
          <th class="num">Removed</th>
          <th class="num">Added mid-cycle</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderSprintReportPanel(subTab, sprint, activeSubTab, emptyStateMessage, emptyTableMessage) {
  const hidden = subTab !== activeSubTab ? " hidden" : "";
  if (!sprint) {
    return `<div class="sprint-subtab-panel${hidden}" data-subtab="${subTab}"><p class="empty-note">${escapeHtml(
      emptyStateMessage
    )}</p></div>`;
  }
  const { cycle, byAssignee } = sprint;
  const table =
    subTab === "previous"
      ? renderPreviousSprintReportAssigneeTable(byAssignee, emptyTableMessage)
      : renderSprintReportAssigneeTable(byAssignee, emptyTableMessage);
  return `
    <div class="sprint-subtab-panel${hidden}" data-subtab="${subTab}">
      <div class="cycle-meta">
        <span class="cycle-name">${escapeHtml(cycleDisplayName(cycle))}</span>
        <span>${formatDate(cycle.startsAt)} → ${formatDate(cycle.endsAt)}</span>
      </div>
      ${table}
    </div>`;
}

function renderSprintReportTeam(squad) {
  const teamKey = squad.team.key;
  const activeSubTab = state.sprintReportSubTab.get(teamKey) || "current";

  const subtabNav = `
    <div class="subtabbar">
      <button type="button" class="subtab-btn${
        activeSubTab === "current" ? " active" : ""
      }" data-team-key="${escapeHtml(teamKey)}" data-subtab="current">Current sprint</button>
      <button type="button" class="subtab-btn${
        activeSubTab === "previous" ? " active" : ""
      }" data-team-key="${escapeHtml(teamKey)}" data-subtab="previous">Previous sprint</button>
    </div>`;

  const currentPanel = renderSprintReportPanel(
    "current",
    squad.currentSprint,
    activeSubTab,
    "No active cycle for this team.",
    "No issues in this cycle."
  );
  const previousPanel = renderSprintReportPanel(
    "previous",
    squad.previousSprint,
    activeSubTab,
    "No completed cycle found for this team.",
    "No issues were assigned."
  );

  return `
    <section class="squad-section" data-team-key="${escapeHtml(teamKey)}">
      <div class="squad-header-static">
        <h2>${escapeHtml(squad.team.name)}</h2>
      </div>
      ${subtabNav}
      ${currentPanel}
      ${previousPanel}
    </section>`;
}

function renderSprintReportTab() {
  if (!els.sprintReportContainer) return;
  const squads = Array.from(state.squadsByKey.values());
  els.sprintReportContainer.innerHTML = squads.length
    ? squads.map(renderSprintReportTeam).join("")
    : '<p class="empty-note">Loading…</p>';
}

// ---- Squad section ----

function renderSquadSection(squad, summitLabel, showSprintData, onlyStarProjects) {
  const teamKey = squad.team.key;
  const collapsed = state.collapsed.has(teamKey);
  const stale = isStale(squad.fetchedAt);

  const sprintBlocks = showSprintData
    ? `${renderCurrentSprintBlock(squad.currentSprint)}${renderPreviousSprintBlock(squad.previousSprint)}`
    : renderSprintDataHiddenBlock();

  return `
    <section class="squad-section${collapsed ? " collapsed" : ""}" data-team-key="${escapeHtml(
    teamKey
  )}">
      <div class="squad-header" data-team-key="${escapeHtml(teamKey)}">
        <div class="squad-header-left">
          <span class="squad-chevron">▾</span>
          <h2>${escapeHtml(squad.team.name)}</h2>
        </div>
        <div class="squad-header-right">
          <span class="updated-at${stale ? " stale" : ""}" data-role="updated-at" title="${new Date(
    squad.fetchedAt * 1000
  ).toLocaleString()}">Updated ${formatRelativeTime(squad.fetchedAt)}</span>
          <button class="btn btn-primary btn-sm squad-update-btn" data-team-key="${escapeHtml(
            teamKey
          )}" type="button">
            <span class="btn-label">Update</span>
          </button>
        </div>
      </div>
      <div class="squad-body${collapsed ? " hidden" : ""}" data-team-key="${escapeHtml(teamKey)}">
        ${renderProjectsBlock(squad, summitLabel, onlyStarProjects)}
        ${renderQualityBlock(squad.quality)}
        ${sprintBlocks}
      </div>
    </section>`;
}

function renderAll() {
  els.squadsContainer.innerHTML = Array.from(state.squadsByKey.values())
    .map((squad) => renderSquadSection(squad, state.summitLabel, state.showSprintData, state.onlyStarProjects))
    .join("");
}

function render(data) {
  state.summitLabel = data.summitLabel;
  state.squadsByKey = new Map(data.squads.map((squad) => [squad.team.key, squad]));
  renderAll();
  renderSprintReportTab();
  els.loadingState.classList.add("hidden");
}

function showError(message) {
  clearSuccess();
  els.errorBanner.textContent = message;
  els.errorBanner.classList.remove("hidden");
}

function clearError() {
  els.errorBanner.classList.add("hidden");
}

function showSuccessHtml(html) {
  clearError();
  els.successBanner.innerHTML = html;
  els.successBanner.classList.remove("hidden");
}

function clearSuccess() {
  els.successBanner.classList.add("hidden");
}

function findSquadSection(teamKey) {
  return els.squadsContainer.querySelector(`.squad-section[data-team-key="${CSS.escape(teamKey)}"]`);
}

function toggleSquad(teamKey) {
  const section = findSquadSection(teamKey);
  if (!section) return;
  const body = section.querySelector(".squad-body");
  const collapsed = !state.collapsed.has(teamKey);
  if (collapsed) {
    state.collapsed.add(teamKey);
  } else {
    state.collapsed.delete(teamKey);
  }
  section.classList.toggle("collapsed", collapsed);
  if (body) body.classList.toggle("hidden", collapsed);
}

async function loadDashboard() {
  clearError();
  try {
    const res = await fetch("/api/dashboard");
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    render(await res.json());
  } catch (err) {
    els.loadingState.classList.add("hidden");
    showError(`Couldn't load dashboard data: ${err.message}`);
  }
}

async function refreshSquad(teamKey) {
  clearError();
  const section = findSquadSection(teamKey);
  const btn = section ? section.querySelector(".squad-update-btn") : null;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span><span class="btn-label">Updating…</span>';
  }
  try {
    const res = await fetch(`/api/dashboard/refresh/${encodeURIComponent(teamKey)}`, {
      method: "POST",
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    const squad = await res.json();
    state.squadsByKey.set(teamKey, squad);
    if (section) {
      section.outerHTML = renderSquadSection(squad, state.summitLabel, state.showSprintData, state.onlyStarProjects);
    }
    renderSprintReportTab();
  } catch (err) {
    showError(`Couldn't update ${teamKey}: ${err.message}`);
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<span class="btn-label">Update</span>';
    }
  }
}

// ---- Notion target bars ----
// Each tab (EPD Report, Sprint Report) has its own "Publishes to" bar
// showing which Notion page its Publish button will create a sub-page
// under. Defaults to `state.notionDefaultParentPageUrl` (the same page
// backend-side, `notion_report.DEFAULT_PARENT_PAGE_URL`) unless the user
// has overridden it via "Edit", in which case the override (per tab) is
// remembered in localStorage - there's no server-side concept of a
// per-tab target, so this is purely a client-side convenience.

const NOTION_TARGET_STORAGE_PREFIX = "productOps.notionTarget.";

function getNotionTarget(tabName) {
  return localStorage.getItem(NOTION_TARGET_STORAGE_PREFIX + tabName) || state.notionDefaultParentPageUrl || "";
}

function setNotionTarget(tabName, url) {
  localStorage.setItem(NOTION_TARGET_STORAGE_PREFIX + tabName, url);
}

function clearNotionTarget(tabName) {
  localStorage.removeItem(NOTION_TARGET_STORAGE_PREFIX + tabName);
}

// Notion page URLs always end in "<slug>-<32-hex-char-id>" - strip the id
// and turn the slug's dashes back into spaces/words for a readable label,
// e.g. ".../Product-Ops-Reports-3be9dd09f47380..." -> "Product Ops Reports".
function notionPageDisplayName(url) {
  try {
    const path = new URL(url).pathname;
    const lastSegment = path.split("/").filter(Boolean).pop() || "";
    const withoutId = lastSegment.replace(/-?[0-9a-f]{32}$/i, "");
    const decoded = decodeURIComponent(withoutId).replace(/-/g, " ").trim();
    return decoded || url;
  } catch (err) {
    return url;
  }
}

function renderNotionTargetBar(bar) {
  const url = getNotionTarget(bar.dataset.notionTab);
  const link = bar.querySelector(".notion-target-link");
  if (url) {
    link.href = url;
    link.textContent = notionPageDisplayName(url);
  } else {
    link.removeAttribute("href");
    link.textContent = "(loading…)";
  }
}

function renderAllNotionTargetBars() {
  els.notionTargetBars.forEach(renderNotionTargetBar);
}

els.notionTargetBars.forEach((bar) => {
  const tabName = bar.dataset.notionTab;
  const editBtn = bar.querySelector(".notion-target-edit-btn");
  const form = bar.querySelector(".notion-target-edit-form");
  const input = bar.querySelector(".notion-target-input");
  const saveBtn = bar.querySelector(".notion-target-save-btn");
  const cancelBtn = bar.querySelector(".notion-target-cancel-btn");
  const resetBtn = bar.querySelector(".notion-target-reset-btn");

  editBtn.addEventListener("click", () => {
    input.value = getNotionTarget(tabName);
    form.classList.remove("hidden");
    input.focus();
    input.select();
  });

  cancelBtn.addEventListener("click", () => {
    form.classList.add("hidden");
  });

  saveBtn.addEventListener("click", () => {
    const value = input.value.trim();
    if (!value) return;
    setNotionTarget(tabName, value);
    renderNotionTargetBar(bar);
    form.classList.add("hidden");
  });

  resetBtn.addEventListener("click", () => {
    clearNotionTarget(tabName);
    renderNotionTargetBar(bar);
    form.classList.add("hidden");
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") saveBtn.click();
    if (event.key === "Escape") cancelBtn.click();
  });
});

renderAllNotionTargetBars();

// ---- Signed-in user (Google sign-in, see product_status/auth.py) ----

// Whether the signed-in user may see the Partner Insights tab, from
// `/api/me`'s `partnerInsightsAccess` - read by `switchTab` too, so it's a
// module-level flag rather than re-derived from the DOM each time. Stays
// false (fail closed) until `/api/me` actually confirms access.
let partnerInsightsAccess = false;

async function loadCurrentUser() {
  try {
    const res = await fetch("/api/me");
    const info = await res.json();
    partnerInsightsAccess = Boolean(info.partnerInsightsAccess);
    if (els.partnerInsightsTabBtn) {
      els.partnerInsightsTabBtn.classList.toggle("hidden", !partnerInsightsAccess);
    }
    // `authenticated: false` means Google sign-in isn't configured at all
    // (see auth.is_configured()) - nothing to show in that case, the app
    // is open to anyone either way.
    if (!info.authenticated) {
      els.topbarUser.classList.add("hidden");
      return;
    }
    els.topbarUserName.textContent = info.name || info.email || "";
    if (info.picture) {
      els.topbarUserAvatar.src = info.picture;
      els.topbarUserAvatar.classList.remove("hidden");
    }
    els.topbarUser.classList.remove("hidden");
  } catch (err) {
    els.topbarUser.classList.add("hidden");
  }
}

// ---- Notion connection (OAuth) ----

async function loadNotionStatus() {
  try {
    const res = await fetch("/api/notion/status");
    const status = await res.json();
    const connected = Boolean(status.connected);
    state.notionDefaultParentPageUrl = status.defaultParentPageUrl || state.notionDefaultParentPageUrl;
    renderAllNotionTargetBars();
    // A static NOTION_API_KEY is a plain env var, not a connect/disconnect
    // flow - there's nothing to "Connect to Notion" or "Disconnect" from,
    // so both of those stay hidden in that case (see notion_oauth.status).
    const viaApiKey = status.method === "api_key";
    els.notionConnectLink.classList.toggle("hidden", connected);
    document.querySelectorAll(".notion-publish-btn").forEach((btn) => btn.classList.toggle("hidden", !connected));
    document.querySelectorAll(".notion-connect-hint").forEach((hint) => hint.classList.toggle("hidden", connected));
    els.notionDisconnectBtn.classList.toggle("hidden", !connected || viaApiKey);
    if (connected) {
      els.notionStatus.textContent = viaApiKey
        ? "Notion: connected (API key)"
        : `Notion: ${status.workspaceName || "connected"}`;
      els.notionStatus.classList.remove("hidden");
    } else {
      els.notionStatus.classList.add("hidden");
    }
  } catch (err) {
    // If the status check itself fails, default to showing "Publish" -
    // the publish call will surface a clear connection error if needed.
    els.notionConnectLink.classList.add("hidden");
    document.querySelectorAll(".notion-publish-btn").forEach((btn) => btn.classList.remove("hidden"));
    document.querySelectorAll(".notion-connect-hint").forEach((hint) => hint.classList.add("hidden"));
  }
}

async function disconnectNotion() {
  clearError();
  clearSuccess();
  try {
    await fetch("/api/notion/disconnect", { method: "POST" });
  } catch (err) {
    // Best-effort; status refresh below reflects reality either way.
  }
  await loadNotionStatus();
}

function handleNotionRedirectParams() {
  const params = new URLSearchParams(window.location.search);
  if (params.has("notion_connected")) {
    showSuccessHtml("Connected to Notion.");
  } else if (params.has("notion_error")) {
    showError(`Couldn't connect to Notion: ${params.get("notion_error")}`);
  } else {
    return;
  }
  window.history.replaceState({}, "", window.location.pathname);
}

els.notionDisconnectBtn.addEventListener("click", disconnectNotion);

async function publishToNotion() {
  clearError();
  clearSuccess();
  const originalLabel = els.notionBtn.innerHTML;
  els.notionBtn.disabled = true;
  els.notionBtn.innerHTML = '<span class="spinner"></span><span class="btn-label">Publishing…</span>';
  try {
    const skipSprintData = !state.showSprintData;
    const onlyStarProjects = state.onlyStarProjects;
    const demoRun = state.demoRun;
    const parentPageUrl = getNotionTarget("epd-report");
    const res = await fetch(
      `/api/dashboard/publish-notion?skip_sprint_data=${skipSprintData}&only_star_projects=${onlyStarProjects}&demo_run=${demoRun}&parent_page_url=${encodeURIComponent(
        parentPageUrl
      )}`,
      { method: "POST" }
    );
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    const result = await res.json();
    if (result.url) {
      showSuccessHtml(
        `Published to Notion: <a href="${escapeHtml(result.url)}" target="_blank" rel="noopener">${escapeHtml(
          result.title || "Open page"
        )}</a>`
      );
    }
  } catch (err) {
    showError(`Couldn't publish to Notion: ${err.message}`);
  } finally {
    els.notionBtn.disabled = false;
    els.notionBtn.innerHTML = originalLabel;
  }
}

els.notionBtn.addEventListener("click", publishToNotion);

async function publishSprintReport() {
  clearError();
  clearSuccess();
  const btn = els.sprintReportNotionBtn;
  if (!btn) return;
  const originalLabel = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span><span class="btn-label">Publishing…</span>';
  try {
    const parentPageUrl = getNotionTarget("sprint-report");
    const res = await fetch(`/api/dashboard/publish-sprint-report?parent_page_url=${encodeURIComponent(parentPageUrl)}`, {
      method: "POST",
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    const result = await res.json();
    if (result.url) {
      showSuccessHtml(
        `Published to Notion: <a href="${escapeHtml(result.url)}" target="_blank" rel="noopener">${escapeHtml(
          result.title || "Open page"
        )}</a>`
      );
    }
  } catch (err) {
    showError(`Couldn't publish sprint report to Notion: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalLabel;
  }
}

if (els.sprintReportNotionBtn) {
  els.sprintReportNotionBtn.addEventListener("click", publishSprintReport);
}

// Drives the web view's own rendering (see `renderAll`), not just what gets
// sent to Notion - so toggling it immediately re-renders every squad
// already loaded, no refetch needed.
els.onlyStarProjectsCheckbox.addEventListener("change", () => {
  state.onlyStarProjects = els.onlyStarProjectsCheckbox.checked;
  renderAll();
});

// Demo run only affects what `publishToNotion` sends - it doesn't change
// the web view, so no re-render here.
els.demoRunCheckbox.addEventListener("change", () => {
  state.demoRun = els.demoRunCheckbox.checked;
});

// Event delegation: squad sections are re-rendered/replaced individually,
// so listeners live on the (stable) container instead of per-section.
els.squadsContainer.addEventListener("click", (event) => {
  const updateBtn = event.target.closest(".squad-update-btn");
  if (updateBtn) {
    event.stopPropagation();
    refreshSquad(updateBtn.dataset.teamKey);
    return;
  }
  const header = event.target.closest(".squad-header");
  if (header) {
    toggleSquad(header.dataset.teamKey);
  }
});

if (els.sprintReportContainer) {
  els.sprintReportContainer.addEventListener("click", (event) => {
    const subtabBtn = event.target.closest(".subtab-btn");
    if (!subtabBtn) return;
    state.sprintReportSubTab.set(subtabBtn.dataset.teamKey, subtabBtn.dataset.subtab);
    renderSprintReportTab();
  });
}

// Keep each squad's "Updated X ago" text ticking without re-fetching data.
setInterval(() => {
  state.squadsByKey.forEach((squad, teamKey) => {
    const section = findSquadSection(teamKey);
    const label = section ? section.querySelector('[data-role="updated-at"]') : null;
    if (label) label.textContent = `Updated ${formatRelativeTime(squad.fetchedAt)}`;
  });
}, 30000);

syncTopbarHeight();
window.addEventListener("resize", syncTopbarHeight);
// Notion connect/publish buttons appearing or disappearing (in the EPD
// Report toolbar) can wrap onto a second line on narrow viewports, which
// doesn't change the topbar/tabbar height itself but re-measuring is cheap
// insurance against that assumption ever changing.
const epdToolbar = document.querySelector(".epd-toolbar");
if (epdToolbar) {
  new MutationObserver(syncTopbarHeight).observe(epdToolbar, {
    childList: true,
    attributes: true,
    subtree: true,
  });
}

// ---- Project Milestones tab ----
// One shared timeline (one row per current-quarter project, milestones
// plotted by target date) plus a callout for anyone who owns multiple
// milestones - across *different* projects - landing close together (see
// `milestones_report.py`'s module docstring for how ownership/overload are
// derived). Loaded lazily (see `switchTab`) since it's a separate, heavier
// Linear pull from the dashboard's own data and may go unvisited.

const MILESTONE_STATUS_CLASS = {
  unstarted: "ms-unstarted",
  next: "ms-next",
  overdue: "ms-overdue",
  done: "ms-done",
};

const MILESTONE_STATUS_LABEL = {
  unstarted: "Unstarted",
  next: "Next",
  overdue: "Overdue",
  done: "Done",
};

// Mirrors `formatDate`'s reasoning: build TimelessDate strings from their
// parts (local midnight) rather than passing them to `new Date()` directly,
// which parses as UTC and can shift a day in timezones behind UTC.
function parseTimelessDate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
  if (!match) return null;
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
}

function daysBetween(a, b) {
  return Math.round((b.getTime() - a.getTime()) / (24 * 60 * 60 * 1000));
}

// Position (0-100) of `dateStr` along the [quarterStart, quarterEnd) axis.
function timelinePercent(dateStr, quarterStart, quarterEnd) {
  const d = parseTimelessDate(dateStr);
  const start = parseTimelessDate(quarterStart);
  const end = parseTimelessDate(quarterEnd);
  if (!d || !start || !end) return null;
  const total = daysBetween(start, end);
  if (total <= 0) return null;
  const pct = (daysBetween(start, d) / total) * 100;
  return Math.round(Math.min(100, Math.max(0, pct)) * 100) / 100;
}

// Weekly (not monthly) ticks - more granular so a specific date is easy to
// pin down once the timeline is wide enough to scroll (see
// `TIMELINE_PX_PER_DAY`/`renderTimelineSection`).
function timelineWeekTicks(quarterStart, quarterEnd) {
  const start = parseTimelessDate(quarterStart);
  const end = parseTimelessDate(quarterEnd);
  if (!start || !end) return [];
  const ticks = [];
  let cursor = new Date(start);
  while (cursor < end) {
    const iso = `${cursor.getFullYear()}-${String(cursor.getMonth() + 1).padStart(2, "0")}-${String(
      cursor.getDate()
    ).padStart(2, "0")}`;
    ticks.push({
      pct: timelinePercent(iso, quarterStart, quarterEnd),
      label: cursor.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
    });
    cursor = new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate() + 7);
  }
  return ticks;
}

function timelineTodayPercent(quarterStart, quarterEnd) {
  const today = new Date();
  const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(
    today.getDate()
  ).padStart(2, "0")}`;
  if (todayStr < quarterStart || todayStr >= quarterEnd) return null;
  return timelinePercent(todayStr, quarterStart, quarterEnd);
}

function milestoneOwnerText(milestone) {
  const names = (milestone.owners || []).map((owner) => owner.name);
  const rolePrefix = milestone.role ? `${milestone.role}: ` : "";
  return names.length ? `${rolePrefix}${names.join(", ")}` : `${rolePrefix}Unassigned`.trim() || "Unassigned";
}

function renderTimelineMarker(milestone, quarterStart, quarterEnd) {
  const pct = timelinePercent(milestone.targetDate, quarterStart, quarterEnd);
  if (pct === null) return "";
  const statusClass = MILESTONE_STATUS_CLASS[milestone.status] || "ms-unstarted";
  const statusLabel = MILESTONE_STATUS_LABEL[milestone.status] || milestone.status || "";
  const tooltip = `${milestone.name} · ${formatDate(milestone.targetDate)} · ${statusLabel} · ${milestoneOwnerText(
    milestone
  )}`;
  const firstOwner = (milestone.owners || [])[0];
  const avatar =
    firstOwner && firstOwner.avatarUrl
      ? `<img class="timeline-marker-avatar" src="${escapeHtml(firstOwner.avatarUrl)}" alt="" />`
      : "";
  return `
    <div class="timeline-marker" style="left: ${pct}%" title="${escapeHtml(tooltip)}">
      <span class="timeline-marker-dot ${statusClass}"></span>
      ${avatar}
      <span class="timeline-marker-label">${escapeHtml(milestone.name)}</span>
    </div>`;
}

// Milestones with no target date can't be plotted on the (date-based)
// track at all, so they're listed out here in the row's label instead of
// just vanishing - see `milestones_report.py`'s module docstring.
function renderMissingList(project) {
  const undated = project.undatedMilestones || [];
  if (!undated.length) return "";
  return `<div class="ms-missing-list">⚠ Missing dates: ${undated.map((m) => escapeHtml(m.name)).join(", ")}</div>`;
}

// Project name is never truncated (see `.timeline-row-label-main a` -
// wraps instead of ellipsizing), and this cell's real height (name +
// status badge + optional missing-dates list) drives its *grid* row's
// height - see `.timeline-cell-label`/`.timeline-grid` in style.css - so
// the corresponding track cell on the same row always matches, with no
// separate height bookkeeping needed.
function renderTimelineRowLabel(project, row) {
  return `
    <div class="timeline-cell-label" style="grid-row: ${row}; grid-column: 1">
      <div class="timeline-row-label-main">
        <a href="${escapeHtml(project.url)}" target="_blank" rel="noopener">${escapeHtml(project.name)}</a>
        <span class="status-badge ${statusBadgeClass(project.statusType)}">${escapeHtml(project.status || "—")}</span>
      </div>
      ${renderMissingList(project)}
    </div>`;
}

function renderTimelineRowTrack(project, quarterStart, quarterEnd, row) {
  const markers = project.milestones.map((m) => renderTimelineMarker(m, quarterStart, quarterEnd)).join("");
  return `<div class="timeline-cell-track" style="grid-row: ${row}; grid-column: 2">${markers}</div>`;
}

// Vertical gridlines + the "today" line - one grid item spanning every
// project row (see `.timeline-vlines` in style.css - CSS grid items are
// allowed to overlap) so they only need rendering once rather than
// per-row, sitting visually behind the marker layer via z-index.
function renderTimelineVlines(quarterStart, quarterEnd, numRows) {
  const lines = timelineWeekTicks(quarterStart, quarterEnd)
    .map((t) => `<div class="timeline-vline" style="left: ${t.pct}%"></div>`)
    .join("");
  const todayPct = timelineTodayPercent(quarterStart, quarterEnd);
  const today = todayPct === null ? "" : `<div class="timeline-today-line" style="left: ${todayPct}%" title="Today"></div>`;
  return `<div class="timeline-vlines" style="grid-row: 2 / span ${numRows}; grid-column: 2">${lines}${today}</div>`;
}

// Date labels for the header row above the tracks.
function renderTimelineHeaderTrack(quarterStart, quarterEnd) {
  const labels = timelineWeekTicks(quarterStart, quarterEnd)
    .map((t) => `<div class="timeline-tick-label" style="left: ${t.pct}%">${escapeHtml(t.label)}</div>`)
    .join("");
  return `<div class="timeline-header-track" style="grid-row: 1; grid-column: 2">${labels}</div>`;
}

// Pixels per day of the quarter the scrollable track area renders at -
// wide enough that weekly ticks/nearby milestones stay legible rather than
// cramming a whole ~92-day quarter into the visible viewport width.
const TIMELINE_PX_PER_DAY = 16;
const TIMELINE_MIN_TRACK_WIDTH = 760;
const TIMELINE_LABEL_WIDTH = 240;

function renderTimelineSection(data) {
  if (!data.projects.length) {
    return '<p class="empty-note">No projects starting this quarter.</p>';
  }
  const start = parseTimelessDate(data.quarterStart);
  const end = parseTimelessDate(data.quarterEnd);
  const totalDays = start && end ? daysBetween(start, end) : 0;
  const trackWidth = Math.max(TIMELINE_MIN_TRACK_WIDTH, Math.round(totalDays * TIMELINE_PX_PER_DAY));

  const rows = data.projects
    .map(
      (p, i) =>
        `${renderTimelineRowLabel(p, i + 2)}${renderTimelineRowTrack(p, data.quarterStart, data.quarterEnd, i + 2)}`
    )
    .join("");

  return `
    <div class="timeline-card">
      <div class="timeline-legend">
        <span><span class="timeline-marker-dot ms-done"></span> Done</span>
        <span><span class="timeline-marker-dot ms-next"></span> Next</span>
        <span><span class="timeline-marker-dot ms-unstarted"></span> Unstarted</span>
        <span><span class="timeline-marker-dot ms-overdue"></span> Overdue</span>
        <span class="timeline-legend-today">Today</span>
        <span class="timeline-legend-hint">Scroll to see more of the quarter →</span>
      </div>
      <div class="timeline-scroll">
        <div class="timeline-grid" style="grid-template-columns: ${TIMELINE_LABEL_WIDTH}px ${trackWidth}px">
          <div class="timeline-cell-label timeline-header-label" style="grid-row: 1; grid-column: 1"></div>
          ${renderTimelineHeaderTrack(data.quarterStart, data.quarterEnd)}
          ${renderTimelineVlines(data.quarterStart, data.quarterEnd, data.projects.length)}
          ${rows}
        </div>
      </div>
    </div>`;
}

function renderOverloadCard(overload) {
  const person = overload.person;
  const avatar = person.avatarUrl
    ? `<img class="overload-avatar" src="${escapeHtml(person.avatarUrl)}" alt="" />`
    : `<span class="overload-avatar overload-avatar-fallback">${escapeHtml((person.name || "?").slice(0, 1))}</span>`;
  const items = overload.milestones
    .map(
      (m) => `
      <li>
        <a href="${escapeHtml(m.projectUrl)}" target="_blank" rel="noopener">${escapeHtml(m.projectName)}</a>
        — ${escapeHtml(m.milestoneName)}${m.role ? ` <span class="label-badge">${escapeHtml(m.role)}</span>` : ""}
        <span class="overload-date">${formatDate(m.targetDate)}</span>
      </li>`
    )
    .join("");
  return `
    <div class="overload-card">
      <div class="overload-card-header">
        ${avatar}
        <div>
          <div class="overload-name">${escapeHtml(person.name)}</div>
          <div class="overload-window">${formatDate(overload.windowStart)} → ${formatDate(overload.windowEnd)}</div>
        </div>
      </div>
      <ul class="overload-list">${items}</ul>
    </div>`;
}

function renderOverloadSection(data) {
  if (!data.overloads.length) {
    return `
      <div class="overload-section overload-section-empty">
        <h3 class="block-title">Overloaded people</h3>
        <p class="empty-note">No one owns multiple milestones (across different projects) within ${data.overloadWindowDays} days of each other this quarter.</p>
      </div>`;
  }
  return `
    <div class="overload-section">
      <h3 class="block-title">Overloaded people <span class="label-badge">milestones within ${data.overloadWindowDays} days, across different projects</span></h3>
      <div class="overload-grid">${data.overloads.map(renderOverloadCard).join("")}</div>
    </div>`;
}

function renderMilestonesReport(data) {
  if (!els.milestonesReportContainer) return;
  els.milestonesReportContainer.innerHTML = `
    ${renderOverloadSection(data)}
    <div class="timeline-section">
      <h3 class="block-title">Timeline</h3>
      ${renderTimelineSection(data)}
    </div>`;
  if (els.milestonesQuarterLabel) {
    els.milestonesQuarterLabel.textContent = `Project Milestones · ${data.quarterLabel}`;
  }
  if (els.milestonesUpdatedAt && data.fetchedAt) {
    els.milestonesUpdatedAt.textContent = `Updated ${formatRelativeTime(data.fetchedAt)}`;
    els.milestonesUpdatedAt.classList.toggle("stale", isStale(data.fetchedAt));
    els.milestonesUpdatedAt.title = new Date(data.fetchedAt * 1000).toLocaleString();
  }
}

let milestonesReportLoaded = false;

async function loadMilestonesReport() {
  if (!els.milestonesReportContainer) return;
  try {
    const res = await fetch("/api/milestones-report");
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    renderMilestonesReport(await res.json());
  } catch (err) {
    els.milestonesReportContainer.innerHTML = `<p class="empty-note">Couldn't load the milestones report: ${escapeHtml(
      err.message
    )}</p>`;
  }
}

async function refreshMilestonesReport() {
  const btn = els.milestonesUpdateBtn;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span><span class="btn-label">Updating…</span>';
  }
  try {
    const res = await fetch("/api/milestones-report/refresh", { method: "POST" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    renderMilestonesReport(await res.json());
  } catch (err) {
    showError(`Couldn't update the milestones report: ${err.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<span class="btn-label">Update</span>';
    }
  }
}

if (els.milestonesUpdateBtn) {
  els.milestonesUpdateBtn.addEventListener("click", refreshMilestonesReport);
}

// ---- Support Report ----

// Mirrors `support_report.py`'s 5 metrics, in the order they're displayed
// (rows of the matrix, one column per squad) - see that module's docstring
// for the exact definitions this ports from the `support-sla-dashboard`
// skill.
const SUPPORT_REPORT_ROWS = [
  { key: "totalOpenKU", label: "Total open Key User tickets" },
  { key: "newKUThisWeek", label: "New Key User tickets this week" },
  { key: "closedKUThisWeek", label: "Key User tickets closed this week" },
  { key: "outOfFirstResponseSLA", label: "Out of first response SLA" },
  { key: "outOfResolutionSLA", label: "Out of resolution SLA" },
];

const SUPPORT_REPORT_SLA_OPTIONS = ["Met", "Not Met", "Pending"];
const SUPPORT_REPORT_PRIORITY_ORDER = ["Urgent", "High", "Medium", "Low", "(blank)"];

// The last-loaded report payload, so clicking a row / typing in a filter can
// re-render without a fresh fetch - see `switchTab`'s lazy-load and
// `refreshSupportReport`.
let supportReportData = null;
// The trend chart's accumulated history log (`{points: [...]}`), fetched
// alongside the main report - see `loadSupportReportHistory`.
let supportReportHistoryData = null;
// Which metric row's ticket list is currently expanded below the table, or
// null if none - toggled by clicking a row (`renderSupportReport`'s click
// handler). Defaults to total open KU so the drill-down table is visible
// on first load.
let supportReportActiveMetric = "totalOpenKU";
// Which sub-tab of the Support Report is showing: "open" (line trend chart +
// counts table + open-tickets table) or "performance" (weekly SLA % bars +
// debug table for the clicked bar). Session-only, like the other view state.
const SUPPORT_REPORT_SUBTABS = [
  { key: "open", label: "Open Tickets" },
  { key: "performance", label: "Performance" },
  { key: "stats", label: "Stats" },
];
let supportReportSubtab = "open";
const supportReportFilters = {
  squad: [],
  createdDateFrom: "",
  createdDateTo: "",
  firstReplyDateFrom: "",
  firstReplyDateTo: "",
  firstResponseSLA: [],
  conversationState: [],
  ticketState: [],
  ticketType: [],
  assignee: [],
  updatedDateFrom: "",
  updatedDateTo: "",
  userName: [],
  partnerName: [],
  priority: [],
  description: "",
};

function formatDateTime(isoString) {
  if (!isoString) return "—";
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatDateOnly(isoString) {
  if (!isoString) return "—";
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

// "Out of first response"/"out of resolution" aren't their own ticket
// lists on the wire - every `openKUTickets` record already carries both
// flags (see `support_report.py`'s docstring), so those two rows are just
// a client-side filter over the same open-ticket list.
function supportReportTicketsForMetric(data, metricKey) {
  const areas = data.areas || [];
  const openTickets = areas.flatMap((area) => (area.metrics && area.metrics.openKUTickets) || []);
  if (metricKey === "newKUThisWeek") {
    return areas.flatMap((area) => (area.metrics && area.metrics.newKUTickets) || []);
  }
  if (metricKey === "closedKUThisWeek") {
    return areas.flatMap((area) => (area.metrics && area.metrics.closedKUTickets) || []);
  }
  if (metricKey === "outOfFirstResponseSLA") {
    return openTickets.filter((t) => t.firstResponseSLA === "Not Met");
  }
  if (metricKey === "outOfResolutionSLA") {
    return openTickets.filter((t) => t.outOfResolutionSLA);
  }
  return openTickets; // totalOpenKU
}

// Intercom conversation state ("open" / "snoozed" / "closed") for display and
// filtering - separate from the ticket status (e.g. "Resolved").
function supportReportConversationStatusLabel(value) {
  if (!value) return "(blank)";
  const text = String(value);
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function supportReportFilterLabel(value) {
  return value || "(blank)";
}

// Ticket Type = Issue Type label of the linked Linear issue(s); "-" when none is
// linked. An empty value means the backend hasn't looked the ticket up yet.
function supportReportTicketTypeLabel(value) {
  return value || "Loading…";
}

function supportReportLocalDayBounds(dateInputValue) {
  if (!dateInputValue) return null;
  const parts = dateInputValue.split("-").map((p) => Number.parseInt(p, 10));
  if (parts.length !== 3 || parts.some((n) => Number.isNaN(n))) return null;
  const [year, month, day] = parts;
  const start = new Date(year, month - 1, day, 0, 0, 0, 0);
  const end = new Date(year, month - 1, day, 23, 59, 59, 999);
  return { start, end };
}

function supportReportTicketInDateFilter(isoString, fromValue, toValue) {
  if (!fromValue && !toValue) return true;
  // No timestamp (e.g. no first reply yet) can't fall inside a date range.
  if (!isoString) return false;
  const created = new Date(isoString);
  if (Number.isNaN(created.getTime())) return false;
  const fromBounds = supportReportLocalDayBounds(fromValue);
  const toBounds = supportReportLocalDayBounds(toValue);
  let rangeStart = fromBounds ? fromBounds.start : null;
  let rangeEnd = toBounds ? toBounds.end : null;
  if (fromBounds && !toBounds) {
    rangeEnd = fromBounds.end;
  }
  if (!fromBounds && toBounds) {
    rangeStart = toBounds.start;
  }
  if (rangeStart && created < rangeStart) return false;
  if (rangeEnd && created > rangeEnd) return false;
  return true;
}

function supportReportIsoDateLocal(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function supportReportFormatFilterDate(iso) {
  const bounds = supportReportLocalDayBounds(iso);
  if (!bounds) return iso;
  return bounds.start.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

const SUPPORT_REPORT_DATE_FILTER_FIELDS = {
  created: { fromKey: "createdDateFrom", toKey: "createdDateTo" },
  firstReply: { fromKey: "firstReplyDateFrom", toKey: "firstReplyDateTo" },
  updated: { fromKey: "updatedDateFrom", toKey: "updatedDateTo" },
};

// Date filters on the other (generic) Support Report tables - see
// `initSupportReportFilterTable` - register themselves here at render time so
// they reuse the exact same range picker; `listeners[field]` re-applies that
// table's filters whenever its range changes.
const supportReportDateFieldListeners = {};
function supportReportRegisterDateField(field, onChange) {
  const fromKey = `${field}From`;
  const toKey = `${field}To`;
  SUPPORT_REPORT_DATE_FILTER_FIELDS[field] = { fromKey, toKey };
  supportReportFilters[fromKey] = "";
  supportReportFilters[toKey] = "";
  supportReportDateFieldListeners[field] = onChange;
}
function supportReportNotifyDateField(field) {
  const listener = supportReportDateFieldListeners[field];
  if (listener) listener();
}

function supportReportDateFilterRange(field) {
  const spec = SUPPORT_REPORT_DATE_FILTER_FIELDS[field];
  return { from: supportReportFilters[spec.fromKey], to: supportReportFilters[spec.toKey] };
}

function supportReportSetDateFilterRange(field, from, to) {
  const spec = SUPPORT_REPORT_DATE_FILTER_FIELDS[field];
  supportReportFilters[spec.fromKey] = from;
  supportReportFilters[spec.toKey] = to;
}

function supportReportClearDateFilter(field) {
  supportReportSetDateFilterRange(field, "", "");
}

function supportReportDateTriggerLabel(field) {
  const { from, to } = supportReportDateFilterRange(field);
  if (!from && !to) return "All dates";
  if (from && (!to || to === from)) return supportReportFormatFilterDate(from);
  if (from && to) return `${supportReportFormatFilterDate(from)} – ${supportReportFormatFilterDate(to)}`;
  if (to) return `Through ${supportReportFormatFilterDate(to)}`;
  return "All dates";
}

let supportReportDateMenuEl = null;
let supportReportDateMenuField = null;
let supportReportDateDraftAnchor = null;
let supportReportDateView = { year: new Date().getFullYear(), month: new Date().getMonth() };

function closeSupportReportDateMenu() {
  if (supportReportDateMenuEl) {
    supportReportDateMenuEl.remove();
    supportReportDateMenuEl = null;
  }
  supportReportDateMenuField = null;
  supportReportDateDraftAnchor = null;
}

function supportReportDateDayClass(iso, viewYear, viewMonth, field) {
  const bounds = supportReportLocalDayBounds(iso);
  if (!bounds) return "day";
  const classes = ["day"];
  if (bounds.start.getFullYear() !== viewYear || bounds.start.getMonth() !== viewMonth) {
    classes.push("other-month");
  }
  const { from, to: toRaw } = supportReportDateFilterRange(field);
  const to = toRaw || from;
  const draft = supportReportDateMenuField === field ? supportReportDateDraftAnchor : null;
  let rangeFrom = from;
  let rangeTo = to;
  if (draft) {
    rangeFrom = draft;
    rangeTo = draft;
  } else if (from && to && from !== to) {
    rangeFrom = from < to ? from : to;
    rangeTo = from < to ? to : from;
  }
  if (rangeFrom && rangeTo) {
    if (iso === rangeFrom) classes.push("range-start");
    else if (iso === rangeTo) classes.push("range-end");
    else if (iso > rangeFrom && iso < rangeTo) classes.push("in-range");
    else if (iso === rangeFrom && iso === rangeTo) classes.push("selected");
  }
  return classes.join(" ");
}

function renderSupportReportDateMenuContent(field) {
  const { year, month } = supportReportDateView;
  const monthLabel = new Date(year, month, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" });
  const firstDow = new Date(year, month, 1).getDay();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const cells = [];
  const dows = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];
  dows.forEach((d) => cells.push(`<span class="dow">${d}</span>`));
  for (let i = 0; i < firstDow; i++) {
    const d = new Date(year, month, -firstDow + i + 1);
    const iso = supportReportIsoDateLocal(d);
    cells.push(
      `<button type="button" class="${supportReportDateDayClass(iso, year, month, field)}" data-date="${iso}">${d.getDate()}</button>`
    );
  }
  for (let day = 1; day <= daysInMonth; day++) {
    const iso = supportReportIsoDateLocal(new Date(year, month, day));
    cells.push(
      `<button type="button" class="${supportReportDateDayClass(iso, year, month, field)}" data-date="${iso}">${day}</button>`
    );
  }
  const trailing = (7 - ((firstDow + daysInMonth) % 7)) % 7;
  for (let i = 1; i <= trailing; i++) {
    const d = new Date(year, month + 1, i);
    const iso = supportReportIsoDateLocal(d);
    cells.push(
      `<button type="button" class="${supportReportDateDayClass(iso, year, month, field)}" data-date="${iso}">${d.getDate()}</button>`
    );
  }
  const hint = supportReportDateDraftAnchor
    ? "Choose end date (or same day for one date)"
    : "Choose start date, then end date";
  return `
    <header>
      <button type="button" data-cal-nav="-1" aria-label="Previous month">‹</button>
      <span>${escapeHtml(monthLabel)}</span>
      <button type="button" data-cal-nav="1" aria-label="Next month">›</button>
    </header>
    <p class="date-range-picker-hint">${hint}</p>
    <div class="date-range-picker-grid">${cells.join("")}</div>
    <div class="date-range-picker-footer">
      <button type="button" data-cal-clear>Clear</button>
      <button type="button" data-cal-close>Close</button>
    </div>`;
}

function refreshSupportReportDateMenu() {
  if (!supportReportDateMenuEl || !supportReportDateMenuField) return;
  supportReportDateMenuEl.innerHTML = renderSupportReportDateMenuContent(supportReportDateMenuField);
}

function updateSupportReportDateTriggers() {
  if (!els.supportReportContainer) return;
  els.supportReportContainer.querySelectorAll(".date-range-picker-trigger[data-date-field]").forEach((btn) => {
    const field = btn.dataset.dateField;
    if (field) btn.textContent = supportReportDateTriggerLabel(field);
  });
}

function openSupportReportDateMenu(trigger) {
  const field = trigger.dataset.dateField;
  if (!field || !SUPPORT_REPORT_DATE_FILTER_FIELDS[field]) return;
  closeSupportReportDateMenu();
  supportReportDateMenuField = field;
  const { from } = supportReportDateFilterRange(field);
  if (from) {
    const b = supportReportLocalDayBounds(from);
    if (b) {
      supportReportDateView = { year: b.start.getFullYear(), month: b.start.getMonth() };
    }
  } else {
    const now = new Date();
    supportReportDateView = { year: now.getFullYear(), month: now.getMonth() };
  }
  supportReportDateMenuEl = document.createElement("div");
  supportReportDateMenuEl.className = "date-range-picker-menu";
  supportReportDateMenuEl.innerHTML = renderSupportReportDateMenuContent(field);
  document.body.appendChild(supportReportDateMenuEl);
  const rect = trigger.getBoundingClientRect();
  supportReportDateMenuEl.style.left = `${rect.left}px`;
  supportReportDateMenuEl.style.top = `${rect.bottom + 4}px`;
  const menuRect = supportReportDateMenuEl.getBoundingClientRect();
  if (menuRect.right > window.innerWidth - 8) {
    supportReportDateMenuEl.style.left = `${Math.max(8, window.innerWidth - menuRect.width - 8)}px`;
  }
}

function handleSupportReportDateMenuClick(event) {
  if (!supportReportDateMenuEl || !supportReportDateMenuField) return;
  const field = supportReportDateMenuField;
  const nav = event.target.closest("[data-cal-nav]");
  if (nav) {
    const delta = Number(nav.getAttribute("data-cal-nav"));
    let { year, month } = supportReportDateView;
    month += delta;
    if (month < 0) {
      month = 11;
      year -= 1;
    } else if (month > 11) {
      month = 0;
      year += 1;
    }
    supportReportDateView = { year, month };
    refreshSupportReportDateMenu();
    return;
  }
  if (event.target.closest("[data-cal-clear]")) {
    supportReportClearDateFilter(field);
    supportReportDateDraftAnchor = null;
    updateSupportReportDateTriggers();
    updateSupportReportDrilldownRows();
    supportReportNotifyDateField(field);
    refreshSupportReportDateMenu();
    return;
  }
  if (event.target.closest("[data-cal-close]")) {
    closeSupportReportDateMenu();
    return;
  }
  const dayBtn = event.target.closest("button.day[data-date]");
  if (!dayBtn) return;
  const iso = dayBtn.getAttribute("data-date");
  if (!supportReportDateDraftAnchor) {
    supportReportDateDraftAnchor = iso;
    refreshSupportReportDateMenu();
    return;
  }
  let from = supportReportDateDraftAnchor;
  let to = iso;
  if (to < from) {
    const tmp = from;
    from = to;
    to = tmp;
  }
  supportReportSetDateFilterRange(field, from, to);
  supportReportDateDraftAnchor = null;
  updateSupportReportDateTriggers();
  updateSupportReportDrilldownRows();
  supportReportNotifyDateField(field);
  closeSupportReportDateMenu();
}

function renderSupportReportDatePicker(field) {
  return `<div class="date-range-picker">
    <button type="button" class="date-range-picker-trigger" data-date-field="${escapeHtml(field)}">${escapeHtml(
    supportReportDateTriggerLabel(field)
  )}</button>
  </div>`;
}

function supportReportFilteredTickets(tickets) {
  const f = supportReportFilters;
  return tickets.filter((t) => {
    if (f.squad.length && !f.squad.includes(t.squadLabel)) return false;
    if (f.firstResponseSLA.length && !f.firstResponseSLA.includes(t.firstResponseSLA)) return false;
    if (f.priority.length && !f.priority.includes(t.priority)) return false;
    if (f.conversationState.length && !f.conversationState.includes(supportReportConversationStatusLabel(t.conversationState)))
      return false;
    if (f.ticketState.length && !f.ticketState.includes(supportReportFilterLabel(t.ticketState))) return false;
    if (f.ticketType.length && !f.ticketType.includes(supportReportTicketTypeLabel(t.ticketType))) return false;
    if (f.assignee.length && !f.assignee.includes(supportReportFilterLabel(t.assignee))) return false;
    if (f.userName.length && !f.userName.includes(supportReportFilterLabel(t.userName))) return false;
    if (f.partnerName.length && !f.partnerName.includes(supportReportFilterLabel(t.partnerName))) return false;
    if (f.description && !(t.description || "").toLowerCase().includes(f.description.toLowerCase())) return false;
    if (!supportReportTicketInDateFilter(t.createdAt, f.createdDateFrom, f.createdDateTo)) {
      return false;
    }
    if (!supportReportTicketInDateFilter(t.firstReplyAt, f.firstReplyDateFrom, f.firstReplyDateTo)) {
      return false;
    }
    if (!supportReportTicketInDateFilter(t.updatedAt, f.updatedDateFrom, f.updatedDateTo)) {
      return false;
    }
    return true;
  });
}

function supportReportMultiSelectTriggerLabel(selected) {
  if (!selected.length) return "All";
  if (selected.length === 1) return selected[0];
  return `${selected.length} selected`;
}

function renderSupportReportMultiSelect(filterKey, options, selected) {
  const selectedSet = new Set(selected);
  const checks = options
    .map(
      (option) => `
      <label class="multi-select-option">
        <input type="checkbox" value="${escapeHtml(option)}"${selectedSet.has(option) ? " checked" : ""}>
        <span class="multi-select-option-label">${escapeHtml(option)}</span>
      </label>`
    )
    .join("");
  return `
    <div class="multi-select-filter" data-filter="${escapeHtml(filterKey)}">
      <button type="button" class="multi-select-trigger">${escapeHtml(
        supportReportMultiSelectTriggerLabel(selected)
      )}</button>
      <div class="multi-select-menu hidden">${checks}</div>
    </div>`;
}

function closeSupportReportMultiSelectMenu(menu) {
  if (!menu) return;
  menu.classList.add("hidden");
  if (menu._multiSelectWrap) {
    menu._multiSelectWrap.appendChild(menu);
    delete menu._multiSelectWrap;
  }
  menu.style.position = "";
  menu.style.left = "";
  menu.style.top = "";
  menu.style.minWidth = "";
  menu.style.width = "";
  menu.style.maxWidth = "";
  menu.style.zIndex = "";
}

function closeAllSupportReportMultiSelectMenus() {
  document.querySelectorAll(".multi-select-menu:not(.hidden)").forEach(closeSupportReportMultiSelectMenu);
}

function openSupportReportMultiSelectMenu(wrap) {
  const menu = wrap.querySelector(".multi-select-menu");
  const trigger = wrap.querySelector(".multi-select-trigger");
  if (!menu || !trigger) return;
  if (!menu.classList.contains("hidden")) {
    closeSupportReportMultiSelectMenu(menu);
    return;
  }
  closeAllSupportReportMultiSelectMenus();
  menu._multiSelectWrap = wrap;
  document.body.appendChild(menu);
  menu.classList.remove("hidden");
  const rect = trigger.getBoundingClientRect();
  menu.style.position = "fixed";
  menu.style.left = `${rect.left}px`;
  menu.style.top = `${rect.bottom + 4}px`;
  menu.style.minWidth = `${Math.max(rect.width, 220)}px`;
  menu.style.width = "max-content";
  menu.style.maxWidth = "320px";
  menu.style.zIndex = "1000";
}

function handleSupportReportMultiSelectChange(checkbox) {
  const menu = checkbox.closest(".multi-select-menu");
  if (!menu) return;
  const wrap = menu._multiSelectWrap || checkbox.closest(".multi-select-filter");
  if (!wrap) return;
  // Generic-table multi-select (see `initSupportReportFilterTable`): it owns
  // its own state through this callback instead of `supportReportFilters`.
  if (wrap._sfOnChange) {
    const values = [...menu.querySelectorAll('input[type="checkbox"]:checked')].map((input) => input.value);
    const trigger = wrap.querySelector(".multi-select-trigger");
    if (trigger) trigger.textContent = supportReportMultiSelectTriggerLabel(values);
    wrap._sfOnChange(values);
    return;
  }
  const filterKey = wrap.dataset.filter;
  if (!filterKey || !Array.isArray(supportReportFilters[filterKey])) return;
  supportReportFilters[filterKey] = [...menu.querySelectorAll('input[type="checkbox"]:checked')].map(
    (input) => input.value
  );
  updateSupportReportMultiSelectTrigger(wrap);
  updateSupportReportDrilldownRows();
}

function updateSupportReportMultiSelectTrigger(wrap) {
  const filterKey = wrap.dataset.filter;
  if (!filterKey || !Array.isArray(supportReportFilters[filterKey])) return;
  const trigger = wrap.querySelector(".multi-select-trigger");
  if (trigger) {
    trigger.textContent = supportReportMultiSelectTriggerLabel(supportReportFilters[filterKey]);
  }
}

function syncTrendColumnSquadFilter() {
  if (supportReportTrendColumn === "TOTAL") {
    supportReportFilters.squad = [];
    return;
  }
  const areas = (supportReportData && supportReportData.areas) || [];
  const area = areas.find((a) => a.squad === supportReportTrendColumn);
  supportReportFilters.squad = area ? [area.label] : [];
}

function updateSupportReportColumnHighlight() {
  if (!els.supportReportContainer) return;
  els.supportReportContainer.querySelectorAll(".support-report-table [data-col-key]").forEach((cell) => {
    cell.classList.toggle("support-col-highlight", cell.dataset.colKey === supportReportTrendColumn);
  });
}

function syncSupportReportSquadFilterUI() {
  const wrap = els.supportReportContainer && els.supportReportContainer.querySelector('.multi-select-filter[data-filter="squad"]');
  if (!wrap) return;
  const selected = new Set(supportReportFilters.squad);
  wrap.querySelectorAll('input[type="checkbox"]').forEach((checkbox) => {
    checkbox.checked = selected.has(checkbox.value);
  });
  updateSupportReportMultiSelectTrigger(wrap);
}

function selectSupportReportTrendColumn(colKey) {
  if (!colKey || supportReportTrendColumn === colKey) return;
  supportReportTrendColumn = colKey;
  syncTrendColumnSquadFilter();
  const radio =
    els.supportReportContainer &&
    els.supportReportContainer.querySelector(`input[name="trend-column"][value="${CSS.escape(colKey)}"]`);
  if (radio) radio.checked = true;
  mountSupportReportTrendChart();
  updateSupportReportColumnHighlight();
  syncSupportReportSquadFilterUI();
  updateSupportReportDrilldownRows();
}

function slaStatusClass(status) {
  if (status === "Met") return "status-completed";
  if (status === "Not Met") return "status-canceled";
  return "status-planned"; // Pending
}

const SUPPORT_REPORT_TICKET_COLUMNS = 13;

// Sortable columns of the drill-down table, in display order. Clicking a
// header cycles ascending -> descending -> unsorted (original order).
const SUPPORT_REPORT_SORT_COLUMNS = [
  { key: "squadLabel", label: "Squad" },
  { key: "createdAt", label: "Date Created", type: "date" },
  { key: "firstReplyAt", label: "First Reply", type: "date" },
  { key: "firstResponseSLA", label: "First Response SLA", type: "sla" },
  { key: "conversationState", label: "Conversation Status", compact: true },
  { key: "ticketState", label: "Ticket Status" },
  { key: "ticketType", label: "Ticket Type" },
  { key: "updatedAt", label: "Last Update", type: "date" },
  { key: "userName", label: "User Name" },
  { key: "partnerName", label: "Partner Name" },
  { key: "assignee", label: "Assignee" },
  { key: "priority", label: "Priority", type: "priority" },
  { key: "description", label: "Ticket Description" },
];
const SUPPORT_REPORT_SLA_SORT_ORDER = ["Not Met", "Pending", "Met"];
const supportReportSort = { key: null, dir: "asc" };

function supportReportSortedTickets(tickets) {
  const { key, dir } = supportReportSort;
  const column = SUPPORT_REPORT_SORT_COLUMNS.find((c) => c.key === key);
  if (!column) return tickets;
  const sign = dir === "desc" ? -1 : 1;
  const valueOf = (t) => {
    const v = t[key];
    if (column.type === "date") {
      const time = v ? new Date(v).getTime() : NaN;
      return Number.isNaN(time) ? null : time;
    }
    if (column.type === "priority") return SUPPORT_REPORT_PRIORITY_ORDER.indexOf(v);
    if (column.type === "sla") return SUPPORT_REPORT_SLA_SORT_ORDER.indexOf(v);
    return (v || "").toString().toLowerCase();
  };
  return [...tickets].sort((a, b) => {
    const va = valueOf(a);
    const vb = valueOf(b);
    // Blanks (e.g. no first reply yet) always sink to the bottom, either direction.
    if (va === null && vb === null) return 0;
    if (va === null) return 1;
    if (vb === null) return -1;
    if (typeof va === "string") return sign * va.localeCompare(vb);
    return sign * (va - vb);
  });
}

function supportReportSortIndicator(key) {
  if (supportReportSort.key !== key) return "";
  return supportReportSort.dir === "asc" ? " ▲" : " ▼";
}

function renderSupportReportSortableHeaders() {
  return SUPPORT_REPORT_SORT_COLUMNS.map(
    (c) =>
      `<th class="sortable${c.compact ? " col-compact" : ""}" data-sort-key="${c.key}" title="Click to sort">${escapeHtml(
        c.label
      )}<span class="sort-indicator">${supportReportSortIndicator(c.key)}</span></th>`
  ).join("");
}

function toggleSupportReportSort(key) {
  if (supportReportSort.key !== key) {
    supportReportSort.key = key;
    supportReportSort.dir = "asc";
  } else if (supportReportSort.dir === "asc") {
    supportReportSort.dir = "desc";
  } else {
    supportReportSort.key = null;
    supportReportSort.dir = "asc";
  }
  if (els.supportReportContainer) {
    els.supportReportContainer.querySelectorAll(".support-drilldown th[data-sort-key]").forEach((th) => {
      const indicator = th.querySelector(".sort-indicator");
      if (indicator) indicator.textContent = supportReportSortIndicator(th.dataset.sortKey);
    });
  }
  updateSupportReportDrilldownRows();
}

function renderSupportReportTicketRows(tickets) {
  if (!tickets.length) {
    return `<tr><td colspan="${SUPPORT_REPORT_TICKET_COLUMNS}"><p class="empty-note">No tickets match these filters.</p></td></tr>`;
  }
  return tickets
    .map(
      (t) => `
      <tr>
        <td>${escapeHtml(t.squadLabel)}</td>
        <td>${formatDateOnly(t.createdAt)}</td>
        <td>${formatDateOnly(t.firstReplyAt)}</td>
        <td><span class="status-badge ${slaStatusClass(t.firstResponseSLA)}">${escapeHtml(
        t.firstResponseSLA
      )}</span></td>
        <td>${escapeHtml(supportReportConversationStatusLabel(t.conversationState))}</td>
        <td>${escapeHtml(supportReportFilterLabel(t.ticketState))}</td>
        <td>${escapeHtml(supportReportTicketTypeLabel(t.ticketType))}</td>
        <td>${formatDateOnly(t.updatedAt)}</td>
        <td>${escapeHtml(t.userName)}</td>
        <td>${escapeHtml(t.partnerName)}</td>
        <td>${escapeHtml(supportReportFilterLabel(t.assignee))}</td>
        <td>${escapeHtml(t.priority)}</td>
        <td><a href="${escapeHtml(t.url)}" target="_blank" rel="noopener">${escapeHtml(t.description)}</a></td>
      </tr>`
    )
    .join("");
}

// Re-renders just the drill-down table's rows + count badge (not the
// filter inputs themselves) so typing in a text filter doesn't steal focus
// away from the input on every keystroke - see the `input`/`change`
// listeners below.
function updateSupportReportDrilldownRows() {
  if (!supportReportData || !supportReportActiveMetric || !els.supportReportContainer) return;
  const tbody = els.supportReportContainer.querySelector(".support-drilldown tbody");
  const badge = els.supportReportContainer.querySelector(".support-drilldown .label-badge");
  if (!tbody) return;
  const allTickets = supportReportTicketsForMetric(supportReportData, supportReportActiveMetric);
  const filtered = supportReportSortedTickets(supportReportFilteredTickets(allTickets));
  tbody.innerHTML = renderSupportReportTicketRows(filtered);
  if (badge) badge.textContent = `${filtered.length} of ${allTickets.length}`;
}

function renderSupportReportDrilldown() {
  if (!supportReportData || !supportReportActiveMetric) return "";
  const row = SUPPORT_REPORT_ROWS.find((r) => r.key === supportReportActiveMetric);
  const allTickets = supportReportTicketsForMetric(supportReportData, supportReportActiveMetric);
  const filtered = supportReportSortedTickets(supportReportFilteredTickets(allTickets));

  // Filter dropdown options are derived from the full (unfiltered) ticket
  // set for this metric, not the currently-filtered one, so options never
  // disappear out from under the user while they're narrowing down.
  const squadOptions = [...new Set(allTickets.map((t) => t.squadLabel))].sort();
  const userNameOptions = [...new Set(allTickets.map((t) => supportReportFilterLabel(t.userName)))].sort();
  const partnerNameOptions = [...new Set(allTickets.map((t) => supportReportFilterLabel(t.partnerName)))].sort();
  const conversationStateOptions = [
    ...new Set(allTickets.map((t) => supportReportConversationStatusLabel(t.conversationState))),
  ].sort();
  const ticketStateOptions = [...new Set(allTickets.map((t) => supportReportFilterLabel(t.ticketState)))].sort();
  const ticketTypeOptions = [...new Set(allTickets.map((t) => supportReportTicketTypeLabel(t.ticketType)))].sort();
  const assigneeOptions = [...new Set(allTickets.map((t) => supportReportFilterLabel(t.assignee)))].sort();
  const priorityOptions = [...new Set(allTickets.map((t) => t.priority))].sort(
    (a, b) => SUPPORT_REPORT_PRIORITY_ORDER.indexOf(a) - SUPPORT_REPORT_PRIORITY_ORDER.indexOf(b)
  );

  return `
    <div class="squad-block support-drilldown">
      <h3 class="block-title">${escapeHtml(row ? row.label : "")} <span class="label-badge">${
    filtered.length
  } of ${allTickets.length}</span></h3>
      <table class="data-table filter-table drilldown-table">
        <thead>
          <tr>${renderSupportReportSortableHeaders()}</tr>
          <tr class="filter-row">
            <th>${renderSupportReportMultiSelect("squad", squadOptions, supportReportFilters.squad)}</th>
            <th>${renderSupportReportDatePicker("created")}</th>
            <th>${renderSupportReportDatePicker("firstReply")}</th>
            <th>${renderSupportReportMultiSelect(
              "firstResponseSLA",
              SUPPORT_REPORT_SLA_OPTIONS,
              supportReportFilters.firstResponseSLA
            )}</th>
            <th>${renderSupportReportMultiSelect(
              "conversationState",
              conversationStateOptions,
              supportReportFilters.conversationState
            )}</th>
            <th>${renderSupportReportMultiSelect("ticketState", ticketStateOptions, supportReportFilters.ticketState)}</th>
            <th>${renderSupportReportMultiSelect("ticketType", ticketTypeOptions, supportReportFilters.ticketType)}</th>
            <th>${renderSupportReportDatePicker("updated")}</th>
            <th>${renderSupportReportMultiSelect("userName", userNameOptions, supportReportFilters.userName)}</th>
            <th>${renderSupportReportMultiSelect(
              "partnerName",
              partnerNameOptions,
              supportReportFilters.partnerName
            )}</th>
            <th>${renderSupportReportMultiSelect("assignee", assigneeOptions, supportReportFilters.assignee)}</th>
            <th>${renderSupportReportMultiSelect("priority", priorityOptions, supportReportFilters.priority)}</th>
            <th><input type="text" data-filter="description" placeholder="Filter…" value="${escapeHtml(
              supportReportFilters.description
            )}"></th>
          </tr>
        </thead>
        <tbody>${renderSupportReportTicketRows(filtered)}</tbody>
      </table>
    </div>`;
}

// One color per SUPPORT_REPORT_ROWS entry, in order - picked to stay
// distinguishable against the dashboard's dark theme (the first four reuse
// the theme's own semantic colors; the 5th is a one-off purple since there's
// no existing 5th semantic color to borrow).
const SUPPORT_REPORT_TREND_COLORS = ["#6e8bff", "#3ecf8e", "#e5c15c", "#f16565", "#b98af6"];

// Weekly cohort SLA bars (right axis, 0–100%) — see support_report.py `_build_weekly_sla_cohorts`.
const SUPPORT_REPORT_WEEKLY_BAR_SERIES = [
  { key: "weeklyPctResolutionMet", label: "% resolution SLA met (weekly cohort)" },
  { key: "weeklyPctFirstResponseMet", label: "% first response SLA met (weekly cohort)" },
];
const SUPPORT_REPORT_WEEKLY_BAR_COLORS = ["#3ecf8e", "#6e8bff"];
// Count series (Stellic responses sent per Pacific day on Key User tickets): a
// line on the Open Tickets chart, sharing its left count axis. Not part of
// SUPPORT_REPORT_WEEKLY_BAR_SERIES because it's a count, not a % of a cohort.
const SUPPORT_REPORT_RESPONSES_SERIES = { key: "dailyStellicResponses", label: "Stellic responses sent" };
const SUPPORT_REPORT_RESPONSES_COLOR = "#f2994a";
// Matches support_report.py `SUPPORT_REPORT_TREND_CHART_MAX_POINTS` (history API returns this many).
const SUPPORT_REPORT_TREND_CHART_MAX_POINTS = 36;

function supportReportTrendChartPoints() {
  const points = (supportReportHistoryData && supportReportHistoryData.points) || [];
  return points.slice(-SUPPORT_REPORT_TREND_CHART_MAX_POINTS);
}

function formatTrendDate(isoString) {
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function formatSupportReportCohortWeek(isoString) {
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return isoString || "";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

// ---- Sortable / filterable tables ----
// Generic enhancer for the Support Report's read-only ticket tables (the SLA
// details panel and the Stats tab's ticket lists - anything marked
// `table.sf-table`): clicking a header cycles ascending -> descending ->
// original order, and a filter row under the headers narrows rows by column
// using the same controls as the Open Tickets drill-down: a multi-select
// checkbox dropdown for ordinary columns, the date-range picker for date
// columns, and a "contains" text box for the ticket-link column. Works on the
// rendered cell text, so each table just needs to render plain rows. (The main
// drill-down table has its own filter state - see `renderSupportReportDrilldown`.)
const SF_RANK = { Urgent: 0, High: 1, Medium: 2, Low: 3, "(blank)": 4 };
const SF_BLANKS = new Set(["", "—", "-", "none", "n/a"]);

// Comparable value for one cell's text: number (also "12%", "5.3 (so far)"),
// timestamp ("Oct 5, 2026[, 3:45 PM]"), priority rank, else lowercased text;
// null for blank-ish cells (always sorted to the bottom).
function sfSortValue(text) {
  const t = (text || "").trim();
  if (SF_BLANKS.has(t.toLowerCase()) || /^not closed/i.test(t)) return null;
  if (Object.prototype.hasOwnProperty.call(SF_RANK, t)) return { kind: "rank", value: SF_RANK[t] };
  if (/^-?\d+(\.\d+)?%?(\s*\(.*\))?$/.test(t)) return { kind: "number", value: parseFloat(t) };
  if (/^[A-Za-z]{3,9}\.? \d{1,2},? \d{4}/.test(t)) {
    const ms = Date.parse(t);
    if (!Number.isNaN(ms)) return { kind: "number", value: ms };
  }
  return { kind: "text", value: t.toLowerCase() };
}

const SF_DATE_RE = /^[A-Za-z]{3,9}\.? \d{1,2},? \d{4}/;
let sfTableCounter = 0;

function initSupportReportFilterTable(table) {
  if (table.dataset.sfReady) return;
  const thead = table.tHead;
  const tbody = table.tBodies[0];
  if (!thead || !tbody || !thead.rows.length) return;
  const headers = [...thead.rows[0].cells];
  const rows = [...tbody.rows].filter((r) => r.cells.length === headers.length);
  if (!rows.length) return;
  table.dataset.sfReady = "1";
  const tableId = ++sfTableCounter;
  rows.forEach((r, i) => {
    r.dataset.sfIndex = String(i);
  });

  const cellText = (row, c) => (row.cells[c].textContent || "").replace(/\s+/g, " ").trim();
  const sortValues = rows.map((r) => headers.map((_, c) => sfSortValue(cellText(r, c))));
  // Per column: "text" (ticket link column -> contains), "date" (range picker,
  // same as Open Tickets) or "multi" (checkbox dropdown, same as Open Tickets).
  const kinds = headers.map((_, c) => {
    if (rows.some((r) => r.cells[c].querySelector("a"))) return "text";
    const texts = rows.map((r) => cellText(r, c)).filter((t) => sfSortValue(t) !== null);
    if (texts.length && texts.every((t) => SF_DATE_RE.test(t) && !Number.isNaN(Date.parse(t)))) return "date";
    return "multi";
  });
  const state = {
    col: null,
    dir: "asc",
    text: headers.map(() => ""),
    multi: headers.map(() => []),
    dateField: headers.map(() => null),
  };

  const emptyRow = document.createElement("tr");
  emptyRow.hidden = true;
  emptyRow.innerHTML = `<td colspan="${headers.length}"><p class="empty-note">No rows match these filters.</p></td>`;
  tbody.appendChild(emptyRow);

  const rowMatches = (row) =>
    headers.every((_, c) => {
      const text = cellText(row, c);
      if (kinds[c] === "text") return !state.text[c] || text.toLowerCase().includes(state.text[c].toLowerCase());
      if (kinds[c] === "multi") return !state.multi[c].length || state.multi[c].includes(text);
      const { from, to } = supportReportDateFilterRange(state.dateField[c]);
      if (!from && !to) return true;
      const ms = Date.parse(text);
      return Number.isNaN(ms) ? false : supportReportTicketInDateFilter(new Date(ms).toISOString(), from, to);
    });

  const apply = () => {
    let ordered = rows.map((row, i) => ({ row, vals: sortValues[i] }));
    if (state.col !== null) {
      const sign = state.dir === "desc" ? -1 : 1;
      const c = state.col;
      const allNumeric = ordered.every(({ vals }) => !vals[c] || vals[c].kind === "number" || vals[c].kind === "rank");
      ordered.sort((a, b) => {
        const va = a.vals[c];
        const vb = b.vals[c];
        if (!va && !vb) return 0;
        if (!va) return 1; // blanks sink either direction
        if (!vb) return -1;
        if (allNumeric) return sign * (va.value - vb.value);
        return sign * String(va.value).localeCompare(String(vb.value), undefined, { numeric: true });
      });
    }
    let visible = 0;
    ordered.forEach(({ row }) => {
      const show = rowMatches(row);
      row.hidden = !show;
      if (show) visible += 1;
      tbody.insertBefore(row, emptyRow);
    });
    emptyRow.hidden = visible > 0;
    headers.forEach((th, c) => {
      const ind = th.querySelector(".sort-indicator");
      if (ind) ind.textContent = state.col === c ? (state.dir === "asc" ? " ▲" : " ▼") : "";
    });
  };

  const filterRow = thead.insertRow();
  filterRow.className = "filter-row";
  headers.forEach((th, c) => {
    th.classList.add("sortable");
    th.title = "Click to sort";
    const indicator = document.createElement("span");
    indicator.className = "sort-indicator";
    th.appendChild(indicator);
    th.addEventListener("click", () => {
      if (state.col !== c) {
        state.col = c;
        state.dir = "asc";
      } else if (state.dir === "asc") {
        state.dir = "desc";
      } else {
        state.col = null;
        state.dir = "asc";
      }
      apply();
    });

    const cell = document.createElement("th");
    if (kinds[c] === "text") {
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = "Filter…";
      input.addEventListener("input", () => {
        state.text[c] = input.value.trim();
        apply();
      });
      cell.appendChild(input);
    } else if (kinds[c] === "date") {
      const field = `sf${tableId}c${c}`;
      state.dateField[c] = field;
      supportReportRegisterDateField(field, apply);
      cell.innerHTML = renderSupportReportDatePicker(field);
    } else {
      const options = [...new Set(rows.map((r) => cellText(r, c)))].sort((a, b) =>
        a.localeCompare(b, undefined, { numeric: true })
      );
      cell.innerHTML = renderSupportReportMultiSelect(`sf${tableId}c${c}`, options, []);
      cell.querySelector(".multi-select-filter")._sfOnChange = (values) => {
        state.multi[c] = values;
        apply();
      };
    }
    filterRow.appendChild(cell);
  });
}

function enhanceSupportReportTables() {
  if (!els.supportReportContainer) return;
  els.supportReportContainer.querySelectorAll("table.sf-table").forEach(initSupportReportFilterTable);
}

// ---- Debug panel (filled when a weekly cohort bar is clicked) ----
// Shows the counts and the exact tickets (with links) behind one bar, using
// the per-ticket detail the backend ships in `weeklyCohorts[].tickets` (see
// support_report.py `_cohort_ticket_debug`).
let supportReportDebugSelection = null; // { weekStartAt (or dayStartAt, for responses), seriesKey, column }

// Open Tickets tab: the tickets Stellic responded to on one Pacific day (from
// `dailyEngagement`, see support_report.py `_build_daily_engagement`).
function renderSupportReportResponsesDebug(sel) {
  const day = ((supportReportData && supportReportData.dailyEngagement) || []).find(
    (d) => d.dayStartAt === sel.weekStartAt
  );
  if (!day) return `<div class="support-debug"></div>`;
  const columnLabel = supportReportTrendColumnLabel(sel.column);
  const reported = (day.byColumn && day.byColumn[sel.column]) || {};
  const respTickets = (day.tickets || []).filter((t) => sel.column === "TOTAL" || t.squad === sel.column);
  const total = respTickets.reduce((sum, t) => sum + t.responses, 0);
  const matches = reported.stellicResponses === total && reported.ticketsResponded === respTickets.length;
  const link = (t) => `<a href="${escapeHtml(t.url)}" target="_blank" rel="noopener">${escapeHtml(t.description)}</a>`;
  const sorted = [...respTickets].sort((a, b) => b.responses - a.responses);
  const rowsHtml = sorted
    .map(
      (t) => `<tr>
          <td>${link(t)}</td>
          <td>${formatDateOnly(t.createdAt)}</td>
          <td>${escapeHtml(t.priority)}</td>
          <td>${escapeHtml(supportReportConversationStatusLabel(t.state))}</td>
          <td>${escapeHtml(t.ticketState || "—")}</td>
          <td class="num">${t.responses}</td>
          <td>${t.lastResponseAt ? formatDateTime(t.lastResponseAt) : "—"}</td>
        </tr>`
    )
    .join("");
  return `
    <div class="squad-block support-debug">
      <h3 class="block-title">${escapeHtml(columnLabel)}: ${escapeHtml(SUPPORT_REPORT_RESPONSES_SERIES.label)} · ${escapeHtml(
    formatSupportReportCohortWeek(day.dayStartAt)
  )}${day.partial ? " (so far today)" : ""}</h3>
      <ul class="debug-stats">
        <li>Stellic responses sent ${day.partial ? "so far today" : "that day"}: <strong>${total}</strong></li>
        <li>Key User tickets responded to: <strong>${respTickets.length}</strong></li>
        <li>Average responses per ticket: <strong>${respTickets.length ? (total / respTickets.length).toFixed(1) : "—"}</strong></li>
        <li>Counted: customer-facing replies by a Stellic teammate (internal notes${
          supportReportData.engagement && supportReportData.engagement.countsBots ? "" : " and bot/Fin replies"
        } excluded), by the Pacific day they were sent — regardless of when the ticket was created or whether it closed.</li>
      </ul>
      <p class="debug-reconcile">${escapeHtml(
        `Chart reported ${reported.stellicResponses ?? "—"} responses on ${reported.ticketsResponded ?? "—"} tickets — ${
          matches ? "✓ matches the listed tickets" : "⚠ does NOT match the listed tickets"
        }`
      )}</p>
      <table class="data-table filter-table sf-table">
        <thead><tr><th>Ticket</th><th>Created</th><th>Priority</th><th class="col-compact">Conversation status</th><th>Ticket status</th>
          <th>Stellic responses that day</th><th>Last response</th></tr></thead>
        <tbody>${rowsHtml || '<tr><td colspan="7"><p class="empty-note">No responses.</p></td></tr>'}</tbody>
      </table>
    </div>`;
}

function renderSupportReportDebug(options = {}) {
  // `responsesOnly` (Open Tickets tab): only the Stellic-responses dots open a
  // panel there, so render an empty hook (for `selectSupportReportDebug`)
  // until one is clicked.
  const sel0 = supportReportDebugSelection;
  if (options.responsesOnly && !(sel0 && sel0.seriesKey === SUPPORT_REPORT_RESPONSES_SERIES.key)) {
    return `<div class="support-debug"></div>`;
  }
  const placeholder = `
    <div class="squad-block support-debug">
      <h3 class="block-title">SLA details</h3>
      <p class="empty-note">Click a bar in the chart above to list the counts and tickets used to calculate it.</p>
    </div>`;
  const sel = supportReportDebugSelection;
  if (!sel || !supportReportData) return placeholder;
  // A responses selection made on the Open Tickets tab has no bar to match here.
  if (sel.seriesKey === SUPPORT_REPORT_RESPONSES_SERIES.key) {
    return options.responsesOnly ? renderSupportReportResponsesDebug(sel) : placeholder;
  }
  const week = (supportReportData.weeklyCohorts || []).find((w) => w.weekStartAt === sel.weekStartAt);
  if (!week) return placeholder;

  const isFirstResponse = sel.seriesKey === "weeklyPctFirstResponseMet";
  const seriesLabel = (SUPPORT_REPORT_WEEKLY_BAR_SERIES.find((s) => s.key === sel.seriesKey) || {}).label || sel.seriesKey;
  const columnLabel = supportReportTrendColumnLabel(sel.column);
  const tickets = (week.tickets || []).filter((t) => sel.column === "TOTAL" || t.squad === sel.column);
  const reported = (week.byColumn && week.byColumn[sel.column]) || {};
  const pct = (num, den) => (den ? `${(Math.round((1000 * num) / den) / 10).toFixed(1)}%` : "—");
  const link = (t) =>
    `<a href="${escapeHtml(t.url)}" target="_blank" rel="noopener">${escapeHtml(t.description)}</a>`;

  let statsHtml;
  let headHtml;
  let rowsHtml;
  let reconcileHtml;

  if (isFirstResponse) {
    const met = tickets.filter((t) => t.frLabel === "Met");
    const notMet = tickets.filter((t) => t.frLabel === "Not Met");
    const pending = tickets.filter((t) => t.frLabel === "Pending");
    const graded = met.length + notMet.length;
    const matches = reported.firstResponseGraded === graded && reported.firstResponseSlaMetCount === met.length;
    statsHtml = `
      <li>Tickets created this week (cohort): <strong>${tickets.length}</strong></li>
      <li>Met (first reply within ${supportReportData.frTargetHours} business hours): <strong>${met.length}</strong></li>
      <li>Not met (replied late, or no reply after the window): <strong>${notMet.length}</strong></li>
      <li>Pending (no reply yet, still inside the window — excluded): <strong>${pending.length}</strong></li>
      <li>Denominator (Met + Not met): <strong>${graded}</strong></li>
      <li>Recomputed: ${met.length} / ${graded} = <strong>${pct(met.length, graded)}</strong></li>`;
    reconcileHtml = `Chart reported ${reported.firstResponseSlaMetCount ?? "—"} / ${
      reported.firstResponseGraded ?? "—"
    } = ${reported.pctFirstResponseSlaMet ?? "—"}% — ${
      matches ? "✓ matches the listed tickets" : "⚠ does NOT match the listed tickets"
    }`;
    const rank = { "Not Met": 0, Pending: 1, Met: 2 };
    const sorted = [...tickets].sort((a, b) => rank[a.frLabel] - rank[b.frLabel]);
    headHtml = `<th>Ticket</th><th>Created</th><th>Priority</th><th class="col-compact">Conversation status</th><th>Ticket status</th>
      <th>First reply</th><th>Reply source</th><th>Business hrs to reply</th><th>Result</th><th>Counted</th>`;
    rowsHtml = sorted
      .map((t) => {
        const counted =
          t.frLabel === "Met" ? "numerator + denominator" : t.frLabel === "Not Met" ? "denominator" : "excluded";
        return `<tr>
          <td>${link(t)}</td>
          <td>${formatDateOnly(t.createdAt)}</td>
          <td>${escapeHtml(t.priority)}</td>
          <td>${escapeHtml(supportReportConversationStatusLabel(t.state))}</td>
          <td>${escapeHtml(t.ticketState || "—")}</td>
          <td>${t.firstReplyAt ? formatDateTime(t.firstReplyAt) : "none"}</td>
          <td>${escapeHtml(t.replySource || "—")}</td>
          <td class="num">${t.frBusinessHours}${t.firstReplyAt ? "" : " (so far)"}</td>
          <td><span class="status-badge ${slaStatusClass(t.frLabel)}">${escapeHtml(t.frLabel)}</span></td>
          <td>${counted}</td>
        </tr>`;
      })
      .join("");
  } else {
    const result = (t) => t.resolutionLabel || "Not eligible";
    const eligible = tickets.filter((t) => t.resolutionEligible);
    const breached = tickets.filter((t) => result(t) === "Breached");
    const metRes = tickets.filter((t) => result(t) === "Met");
    const pendingRes = tickets.filter((t) => result(t) === "Pending");
    const matches =
      reported.resolutionEligible === eligible.length &&
      reported.resolutionSlaMetCount === metRes.length &&
      reported.resolutionPending === pendingRes.length;
    const potentialMet = metRes.length + pendingRes.length;
    statsHtml = `
      <li>Tickets created this week (cohort): <strong>${tickets.length}</strong></li>
      <li>Not Urgent/High (not eligible — excluded): <strong>${tickets.length - eligible.length}</strong></li>
      <li>Eligible (Urgent/High) — denominator: <strong>${eligible.length}</strong></li>
      <li>Met (closed within ${supportReportData.resTargetDays} days) — numerator: <strong>${metRes.length}</strong></li>
      <li>Pending (still open, under ${supportReportData.resTargetDays} days — in the denominator, not yet in the numerator): <strong>${pendingRes.length}</strong></li>
      <li>Breached (took, or has so far taken, more than ${supportReportData.resTargetDays} days): <strong>${breached.length}</strong></li>
      <li>Evaluated as of: <strong>${formatDateTime(week.evaluatedAt)}</strong></li>
      <li>Recomputed: ${metRes.length} / ${eligible.length} = <strong>${pct(metRes.length, eligible.length)}</strong>${
        pendingRes.length ? ` (up to ${pct(potentialMet, eligible.length)} if every pending ticket is met)` : ""
      }</li>`;
    reconcileHtml = `Chart reported ${reported.resolutionSlaMetCount ?? "—"} / ${
      reported.resolutionEligible ?? "—"
    } = ${reported.pctResolutionSlaMet ?? "—"}% (${reported.resolutionPending ?? "—"} pending) — ${
      matches ? "✓ matches the listed tickets" : "⚠ does NOT match the listed tickets"
    }`;
    const rank = { Breached: 0, Pending: 1, Met: 2, "Not eligible": 3 };
    const sorted = [...tickets].sort((a, b) => rank[result(a)] - rank[result(b)]);
    headHtml = `<th>Ticket</th><th>Created</th><th>Priority</th><th class="col-compact">Conversation status</th><th>Ticket status</th>
      <th>Closed at</th><th>Close time source</th><th>Age at evaluation (days)</th><th>Result</th><th>Counted</th>`;
    rowsHtml = sorted
      .map((t) => {
        const r = result(t);
        const badge = r === "Met" ? "status-completed" : r === "Breached" ? "status-canceled" : "status-planned";
        const counted =
          r === "Met"
            ? "numerator + denominator"
            : r === "Breached"
              ? "denominator"
              : r === "Pending"
                ? "denominator (not yet met)"
                : "excluded";
        return `<tr>
          <td>${link(t)}</td>
          <td>${formatDateOnly(t.createdAt)}</td>
          <td>${escapeHtml(t.priority)}</td>
          <td>${escapeHtml(supportReportConversationStatusLabel(t.state))}</td>
          <td>${escapeHtml(t.ticketState || "—")}</td>
          <td>${t.closedAt ? formatDateTime(t.closedAt) : "not closed (clock still running)"}</td>
          <td>${escapeHtml(t.closedSource || "—")}</td>
          <td class="num">${t.resolutionAgeDays ?? "—"}</td>
          <td><span class="status-badge ${badge}">${escapeHtml(r)}</span></td>
          <td>${counted}</td>
        </tr>`;
      })
      .join("");
  }

  return `
    <div class="squad-block support-debug">
      <h3 class="block-title">${escapeHtml(columnLabel)}: ${escapeHtml(seriesLabel)} · week of ${escapeHtml(
    formatSupportReportCohortWeek(week.weekStartAt)
  )}</h3>
      <ul class="debug-stats">${statsHtml}</ul>
      <p class="debug-reconcile">${escapeHtml(reconcileHtml)}</p>
      <table class="data-table filter-table sf-table">
        <thead><tr>${headHtml}</tr></thead>
        <tbody>${rowsHtml || '<tr><td colspan="10"><p class="empty-note">No tickets in this cohort.</p></td></tr>'}</tbody>
      </table>
    </div>`;
}

function selectSupportReportDebug(selection) {
  supportReportDebugSelection = selection;
  const el = els.supportReportContainer && els.supportReportContainer.querySelector(".support-debug");
  if (!el) return;
  el.outerHTML = renderSupportReportDebug({ responsesOnly: supportReportSubtab !== "performance" });
  enhanceSupportReportTables();
  const fresh = els.supportReportContainer.querySelector(".support-debug");
  if (fresh) fresh.scrollIntoView({ behavior: "smooth", block: "start" });
}

// "YYYY-MM-DD" of an ISO timestamp in Pacific time (matches `dailyEngagement[].date`).
function supportReportPacificDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Los_Angeles",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(d);
}

// "Oct 8, 2026" for the Pacific calendar day a snapshot belongs to (the same
// day the daily trend points and the Stellic responses series are keyed on).
function supportReportPacificDayLabel(iso) {
  const d = new Date(iso);
  if (!iso || Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-US", {
    timeZone: "America/Los_Angeles",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function supportReportResponsesTooltip({ columnLabel, dayStartAt, partial, responses, tickets }) {
  return [
    columnLabel,
    `${formatSupportReportCohortWeek(dayStartAt)}${partial ? " (so far today)" : ""}`,
    `Stellic responses sent: ${responses}`,
    `Across ${tickets} Key User ticket${tickets === 1 ? "" : "s"}`,
    "Click for the tickets",
  ].join("\n");
}

function supportReportWeeklyBarTooltip({ columnLabel, weekStartAt, seriesLabel, total, met, pct, pending, potential }) {
  const hasPending = typeof pending === "number" && pending > 0;
  const lines = [
    columnLabel,
    `Week of ${formatSupportReportCohortWeek(weekStartAt)}`,
    typeof pending === "number" ? `Eligible tickets (Urgent/High): ${total}` : `Tickets graded (pending excluded): ${total}`,
    `Met SLA: ${met}`,
  ];
  if (typeof pending === "number") lines.push(`Pending (open, not yet past SLA): ${pending}`);
  lines.push(`${seriesLabel}: ${pct}%${hasPending ? " so far" : ""}`);
  if (hasPending && potential != null) lines.push(`Up to ${potential}% if every pending ticket is met`);
  lines.push("Click for calculation details");
  return lines.join("\n");
}

// Builds the actual <svg>...</svg> markup for the trend chart at a given
// pixel width. Called with the *real* measured container width (see
// `mountSupportReportTrendChart`) so the SVG's viewBox maps 1:1 to on-screen
// pixels - avoids the classic responsive-SVG trap where a fixed viewBox
// scaled to fill a flexible-width container via `preserveAspectRatio="none"`
// stretches circles into ellipses and warps text.
function renderSupportReportTrendSVG(points, width, column, columnLabel, hiddenSeriesKeys, weeklyCohorts, engagementDays) {
  const height = 220;
  const paddingLeft = 44;
  const paddingRight = 44;
  const paddingTop = 14;
  const paddingBottom = 26;
  const plotWidth = Math.max(1, width - paddingLeft - paddingRight);
  const plotHeight = height - paddingTop - paddingBottom;
  const n = points.length;

  const times = points.map((p) => new Date(p.at).getTime());
  const leftStep = n >= 2 ? times[1] - times[0] : 86400000;
  const rightStep = n >= 2 ? times[n - 1] - times[n - 2] : 86400000;
  const virtualLeftTime = times[0] - leftStep;
  const virtualRightTime = times[n - 1] + rightStep;
  const weekCenters = (weeklyCohorts || [])
    .map((w) => new Date(w.weekStartAt).getTime() + 3.5 * 86400000)
    .filter((t) => !Number.isNaN(t));
  let timeMin = Math.min(virtualLeftTime, ...times, ...(weekCenters.length ? weekCenters : [virtualLeftTime]));
  let timeMax = Math.max(virtualRightTime, ...times, ...(weekCenters.length ? weekCenters : [virtualRightTime]));
  if (timeMax <= timeMin) timeMax = timeMin + 86400000;
  const timeSpan = timeMax - timeMin;
  const xFromTime = (t) => paddingLeft + ((t - timeMin) / timeSpan) * plotWidth;
  const xFor = (dataIndex) => xFromTime(times[dataIndex]);
  const virtualLeftAt = new Date(virtualLeftTime).toISOString();
  const virtualRightAt = new Date(virtualRightTime).toISOString();

  // Series hidden via the legend (see `renderSupportReportTrendChart`'s
  // clickable legend items) are dropped entirely here - not just visually
  // suppressed - so the y-axis rescales to whatever's still showing rather
  // than leaving dead space sized for a line that's currently off.
  const series = SUPPORT_REPORT_ROWS.map((row, idx) => ({
    key: row.key,
    label: row.label,
    color: SUPPORT_REPORT_TREND_COLORS[idx % SUPPORT_REPORT_TREND_COLORS.length],
    values: points.map((p) => ((p.metrics && p.metrics[row.key] && p.metrics[row.key][column]) || 0)),
  })).filter((s) => !hiddenSeriesKeys || !hiddenSeriesKeys.has(s.key));

  const showBars =
    weeklyCohorts &&
    weeklyCohorts.length &&
    SUPPORT_REPORT_WEEKLY_BAR_SERIES.some((s) => !hiddenSeriesKeys || !hiddenSeriesKeys.has(s.key));
  // Stellic responses: counts, so they share the left axis with the other
  // count lines. One point per refresh snapshot, at the snapshot's own x - the
  // value is the number of responses sent on that snapshot's Pacific calendar
  // day (final figure from the latest report, so it isn't cut off at the time
  // of day the snapshot was taken). Days with no snapshot get no point, so the
  // line lines up with, and spans the same range as, the other series.
  const showResponses =
    !!(engagementDays && engagementDays.length) &&
    (!hiddenSeriesKeys || !hiddenSeriesKeys.has(SUPPORT_REPORT_RESPONSES_SERIES.key));
  const responseDays = showResponses
    ? points
        .map((p, i) => {
          const d = engagementDays.find((day) => day.date === supportReportPacificDate(p.at));
          if (!d) return null;
          const col = (d.byColumn && d.byColumn[column]) || {};
          return {
            d,
            x: xFor(i),
            v: typeof col.stellicResponses === "number" ? col.stellicResponses : null,
            tickets: col.ticketsResponded || 0,
          };
        })
        .filter((p) => p && p.v != null)
    : [];
  if (series.length === 0 && !showBars && !responseDays.length) {
    return `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" class="trend-svg">
      <text x="${width / 2}" y="${height / 2}" text-anchor="middle" class="trend-axis-label">Every series is hidden — click a legend item to show it again.</text>
    </svg>`;
  }

  const maxValue = Math.max(1, ...series.flatMap((s) => s.values), ...responseDays.map((p) => p.v));
  const yFor = (v) => paddingTop + plotHeight - (v / maxValue) * plotHeight;
  const yPct = (pct) => paddingTop + plotHeight - (pct / 100) * plotHeight;

  const gridLines = [0, 0.25, 0.5, 0.75, 1]
    .map((t) => {
      const y = paddingTop + plotHeight - t * plotHeight;
      return `<line x1="${paddingLeft}" y1="${y.toFixed(1)}" x2="${width - paddingRight}" y2="${y.toFixed(
        1
      )}" class="trend-gridline" />
        <text x="${paddingLeft - 8}" y="${(y + 4).toFixed(1)}" class="trend-axis-label" text-anchor="end">${Math.round(
        t * maxValue
      )}</text>`;
    })
    .join("");

  const rightAxis =
    showBars &&
    [0, 0.25, 0.5, 0.75, 1]
      .map((t) => {
        const y = paddingTop + plotHeight - t * plotHeight;
        return `<text x="${width - paddingRight + 8}" y="${(y + 4).toFixed(
          1
        )}" class="trend-axis-label trend-axis-label-right" text-anchor="start">${Math.round(t * 100)}%</text>`;
      })
      .join("");

  const maxLabels = Math.min(n, Math.max(2, Math.floor(plotWidth / 90)));
  const labelStep = Math.max(1, Math.round((n - 1) / Math.max(1, maxLabels - 1)));
  const realLabels = points
    .map((p, i) => ({ at: p.at, i }))
    .filter(({ i }) => i % labelStep === 0 || i === n - 1)
    .map(
      ({ at }) =>
        `<text x="${xFromTime(new Date(at).getTime()).toFixed(1)}" y="${height - 6}" class="trend-axis-label" text-anchor="middle">${escapeHtml(
          formatTrendDate(at)
        )}</text>`
    )
    .join("");
  const virtualLabel = (at) =>
    `<text x="${xFromTime(new Date(at).getTime()).toFixed(1)}" y="${
      height - 6
    }" class="trend-axis-label trend-axis-label-faint" text-anchor="middle">${escapeHtml(formatTrendDate(at))}</text>`;
  const xLabels = virtualLabel(virtualLeftAt) + realLabels + virtualLabel(virtualRightAt);

  const weekBarWidth = Math.max(4, (7 * 86400000 / timeSpan) * plotWidth * 0.18);
  const barSeriesVisible = SUPPORT_REPORT_WEEKLY_BAR_SERIES.filter(
    (s) => !hiddenSeriesKeys || !hiddenSeriesKeys.has(s.key)
  );
  const barsSvg =
    showBars && barSeriesVisible.length
      ? weeklyCohorts
          .map((w) => {
            const col = (w.byColumn && w.byColumn[column]) || {};
            const weekStart = new Date(w.weekStartAt).getTime();
            if (Number.isNaN(weekStart)) return "";
            const cx = xFromTime(weekStart + 3.5 * 86400000);
            const values = [
              {
                key: "weeklyPctResolutionMet",
                pct: col.pctResolutionSlaMet,
                total: col.resolutionEligible,
                met: col.resolutionSlaMetCount,
                color: SUPPORT_REPORT_WEEKLY_BAR_COLORS[0],
                label: SUPPORT_REPORT_WEEKLY_BAR_SERIES[0].label,
              },
              {
                key: "weeklyPctFirstResponseMet",
                pct: col.pctFirstResponseSlaMet,
                total: col.firstResponseGraded,
                met: col.firstResponseSlaMetCount,
                color: SUPPORT_REPORT_WEEKLY_BAR_COLORS[1],
                label: SUPPORT_REPORT_WEEKLY_BAR_SERIES[1].label,
              },
            ].filter(
              (v) =>
                barSeriesVisible.some((s) => s.key === v.key) &&
                v.pct != null &&
                typeof v.total === "number" &&
                typeof v.met === "number"
            );
            if (!values.length) return "";
            const groupWidth = weekBarWidth * values.length + 2 * (values.length - 1);
            let x = cx - groupWidth / 2;
            return values
              .map((v) => {
                const y0 = paddingTop + plotHeight;
                const y1 = yPct(v.pct);
                const h = Math.max(0, y0 - y1);
                const rect = `<rect x="${x.toFixed(1)}" y="${y1.toFixed(1)}" width="${weekBarWidth.toFixed(
                  1
                )}" height="${h.toFixed(1)}" fill="${v.color}" opacity="0.55" rx="1"></rect>`;
                const tip = escapeHtml(
                  supportReportWeeklyBarTooltip({
                    columnLabel,
                    weekStartAt: w.weekStartAt,
                    seriesLabel: v.label,
                    total: v.total,
                    met: v.met,
                    pct: v.pct,
                  })
                );
                const hit = `<rect class="trend-bar-hit" x="${x.toFixed(1)}" y="${paddingTop}" width="${weekBarWidth.toFixed(
                  1
                )}" height="${plotHeight}" fill="transparent" data-tooltip="${tip}" data-week="${escapeHtml(
                  w.weekStartAt
                )}" data-series="${escapeHtml(v.key)}" data-column="${escapeHtml(column)}"></rect>`;
                x += weekBarWidth + 2;
                return rect + hit;
              })
              .join("");
          })
          .join("")
      : "";

  const seriesSvg = series
    .map((s) => {
      const path = s.values.map((v, i) => `${i === 0 ? "M" : "L"}${xFor(i).toFixed(1)},${yFor(v).toFixed(1)}`).join(" ");
      const dots = s.values
        .map((v, i) => {
          const cx = xFor(i).toFixed(1);
          const cy = yFor(v).toFixed(1);
          const day = supportReportPacificDayLabel(points[i] && points[i].at);
          const tooltip = `${escapeHtml(columnLabel)} — ${escapeHtml(s.label)}: ${v}${day ? `\n${escapeHtml(day)}` : ""}`;
          // Two circles per point: a small visible dot, plus a larger
          // invisible one layered on top purely to give the mouse a bigger,
          // more reliable hit target (see `attachTrendTooltipHandlers` -
          // native SVG <title> tooltips are inconsistent across browsers,
          // notably Safari, so hover is handled with real mouse events
          // instead).
          return (
            `<circle cx="${cx}" cy="${cy}" r="3" fill="${s.color}" style="pointer-events:none"></circle>` +
            `<circle class="trend-dot-hit" cx="${cx}" cy="${cy}" r="8" fill="transparent" data-tooltip="${tooltip}"></circle>`
          );
        })
        .join("");
      return `<path d="${path}" fill="none" stroke="${s.color}" stroke-width="2" style="pointer-events:none" />${dots}`;
    })
    .join("");

  let responsesSvg = "";
  if (responseDays.length) {
    const color = SUPPORT_REPORT_RESPONSES_COLOR;
    const path = responseDays
      .map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${yFor(p.v).toFixed(1)}`)
      .join(" ");
    const dots = responseDays
      .map((p) => {
        const cx = p.x.toFixed(1);
        const cy = yFor(p.v).toFixed(1);
        const tip = escapeHtml(
          supportReportResponsesTooltip({
            columnLabel,
            dayStartAt: p.d.dayStartAt,
            partial: !!p.d.partial,
            responses: p.v,
            tickets: p.tickets,
          })
        );
        return (
          `<circle cx="${cx}" cy="${cy}" r="3" fill="${color}" style="pointer-events:none"></circle>` +
          `<circle class="trend-bar-hit" cx="${cx}" cy="${cy}" r="8" fill="transparent" data-tooltip="${tip}" data-week="${escapeHtml(
            p.d.dayStartAt
          )}" data-series="${escapeHtml(SUPPORT_REPORT_RESPONSES_SERIES.key)}" data-column="${escapeHtml(column)}" style="cursor:pointer"></circle>`
        );
      })
      .join("");
    responsesSvg = `<path d="${path}" fill="none" stroke="${color}" stroke-width="2" style="pointer-events:none" />${dots}`;
  }

  return `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" class="trend-svg">${gridLines}${rightAxis || ""}${barsSvg}${seriesSvg}${responsesSvg}${xLabels}</svg>`;
}

let trendTooltipEl = null;

// Wires up hover handling for a freshly-drawn trend chart's hit-target
// circles (see `renderSupportReportTrendSVG`). Uses real mouse events +
// a plain positioned div rather than native SVG <title> tooltips, which
// render inconsistently (or not at all, e.g. in Safari) and have an
// awkward built-in delay.
function positionTrendTooltip(tooltip, evt) {
  const margin = 12;
  const gap = 10;
  tooltip.style.display = "block";
  tooltip.style.visibility = "hidden";
  tooltip.style.left = "0px";
  tooltip.style.top = "0px";
  const tipRect = tooltip.getBoundingClientRect();
  let left = evt.clientX - tipRect.width / 2;
  let top = evt.clientY - tipRect.height - gap;
  if (left + tipRect.width > window.innerWidth - margin) {
    left = evt.clientX - tipRect.width - 8;
  }
  if (left < margin) {
    left = margin;
  }
  if (top < margin) {
    top = evt.clientY + gap;
  }
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${top}px`;
  tooltip.style.visibility = "visible";
}

function attachTrendTooltipHandlers(wrap) {
  if (!trendTooltipEl) {
    trendTooltipEl = document.createElement("div");
    trendTooltipEl.className = "trend-tooltip";
    document.body.appendChild(trendTooltipEl);
  }
  const tooltip = trendTooltipEl;
  const hide = () => {
    tooltip.style.display = "none";
  };
  hide();
  wrap.querySelectorAll(".trend-dot-hit, .trend-bar-hit, .trend-stat-hit").forEach((dot) => {
    dot.addEventListener("mouseenter", (evt) => {
      tooltip.textContent = dot.getAttribute("data-tooltip") || "";
      positionTrendTooltip(tooltip, evt);
    });
    dot.addEventListener("mousemove", (evt) => positionTrendTooltip(tooltip, evt));
    dot.addEventListener("mouseleave", hide);
  });
}

// Re-renders just the legend (for its hidden/active styling) and redraws
// the chart after a legend-item click toggles `supportReportTrendHiddenSeries`
// - deliberately scoped to these two pieces rather than the whole tab so the
// ticket drilldown/filters below aren't blown away by an unrelated click.
function refreshSupportReportTrendLegendAndChart() {
  if (supportReportSubtab === "stats" && supportReportData) {
    renderSupportReport(supportReportData);
    return;
  }
  const legendEl = els.supportReportContainer && els.supportReportContainer.querySelector(".trend-legend");
  if (legendEl) legendEl.innerHTML = renderSupportReportTrendLegend();
  mountSupportReportTrendChart();
}

// Bars-only chart for the Performance sub-tab: one group per weekly cohort
// (evenly spaced, labelled by week start), % on a single 0-100 axis. Reuses
// the same `.trend-bar-hit` / `data-week|series|column` hooks as the combo
// chart so hover tooltips and click-for-debug work unchanged.
function renderSupportReportPerformanceSVG(weeklyCohorts, width, column, columnLabel, hiddenSeriesKeys) {
  const height = 220;
  const paddingLeft = 44;
  const paddingRight = 16;
  const paddingTop = 18;
  const paddingBottom = 26;
  const plotWidth = Math.max(1, width - paddingLeft - paddingRight);
  const plotHeight = height - paddingTop - paddingBottom;
  const yPct = (pct) => paddingTop + plotHeight - (pct / 100) * plotHeight;

  const visibleSeries = SUPPORT_REPORT_WEEKLY_BAR_SERIES.map((s, idx) => ({
    ...s,
    color: SUPPORT_REPORT_WEEKLY_BAR_COLORS[idx],
  })).filter((s) => !hiddenSeriesKeys || !hiddenSeriesKeys.has(s.key));
  if (!visibleSeries.length) {
    return `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" class="trend-svg">
      <text x="${width / 2}" y="${height / 2}" text-anchor="middle" class="trend-axis-label">Every series is hidden — click a legend item to show it again.</text>
    </svg>`;
  }

  const weeks = [...weeklyCohorts].sort((a, b) => new Date(a.weekStartAt) - new Date(b.weekStartAt));
  const slot = plotWidth / Math.max(1, weeks.length);
  const barWidth = Math.max(10, Math.min(48, (slot * 0.7) / visibleSeries.length));
  const gap = 4;
  const groupWidth = barWidth * visibleSeries.length + gap * (visibleSeries.length - 1);

  const gridLines = [0, 0.25, 0.5, 0.75, 1]
    .map((t) => {
      const y = paddingTop + plotHeight - t * plotHeight;
      return `<line x1="${paddingLeft}" y1="${y.toFixed(1)}" x2="${width - paddingRight}" y2="${y.toFixed(
        1
      )}" class="trend-gridline" />
        <text x="${paddingLeft - 8}" y="${(y + 4).toFixed(1)}" class="trend-axis-label" text-anchor="end">${Math.round(
        t * 100
      )}%</text>`;
    })
    .join("");

  const groups = weeks
    .map((w, i) => {
      const col = (w.byColumn && w.byColumn[column]) || {};
      const cx = paddingLeft + slot * (i + 0.5);
      const label = `<text x="${cx.toFixed(1)}" y="${height - 6}" class="trend-axis-label" text-anchor="middle">${escapeHtml(
        formatTrendDate(w.weekStartAt)
      )}</text>`;
      const values = visibleSeries.map((s) => {
        const isRes = s.key === "weeklyPctResolutionMet";
        return {
          key: s.key,
          label: s.label,
          color: s.color,
          pct: isRes ? col.pctResolutionSlaMet : col.pctFirstResponseSlaMet,
          total: isRes ? col.resolutionEligible : col.firstResponseGraded,
          met: isRes ? col.resolutionSlaMetCount : col.firstResponseSlaMetCount,
          pending: isRes ? col.resolutionPending : undefined,
          potential: isRes ? col.pctResolutionSlaPotential : undefined,
        };
      });
      let x = cx - groupWidth / 2;
      const bars = values
        .map((v) => {
          const hasData = v.pct != null && typeof v.total === "number" && typeof v.met === "number";
          const bx = x;
          x += barWidth + gap;
          if (!hasData) {
            return `<text x="${(bx + barWidth / 2).toFixed(1)}" y="${yPct(0) - 4}" class="trend-axis-label trend-axis-label-faint" text-anchor="middle">n/a</text>`;
          }
          const y1 = yPct(v.pct);
          const h = Math.max(0, paddingTop + plotHeight - y1);
          const tip = escapeHtml(
            supportReportWeeklyBarTooltip({
              columnLabel,
              weekStartAt: w.weekStartAt,
              seriesLabel: v.label,
              total: v.total,
              met: v.met,
              pct: v.pct,
              pending: v.pending,
              potential: v.potential,
            })
          );
          // In-progress share (open tickets that could still be met): a
          // hatched cap above the solid bar up to the best-case %.
          const inProgress = typeof v.pending === "number" && v.pending > 0 && v.potential != null && v.potential > v.pct;
          const yTop = inProgress ? yPct(v.potential) : y1;
          const cap = inProgress
            ? `<rect x="${bx.toFixed(1)}" y="${yTop.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${Math.max(
                0,
                y1 - yTop
              ).toFixed(1)}" fill="url(#perf-hatch-${v.key})" stroke="${v.color}" stroke-width="1" stroke-dasharray="3 2" opacity="0.9" rx="2"></rect>`
            : "";
          return (
            `<rect x="${bx.toFixed(1)}" y="${y1.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${h.toFixed(
              1
            )}" fill="${v.color}" opacity="0.75" rx="2"></rect>` +
            cap +
            // In-progress bars: the % sits just under the line where the
            // hatched cap starts, in dark text so it reads over the green
            // (falls back to above the cap when the solid part is too short
            // to hold it). Settled bars keep the label above the bar.
            (inProgress && h >= 16
              ? `<text x="${(bx + barWidth / 2).toFixed(1)}" y="${(y1 + 13).toFixed(
                  1
                )}" class="trend-axis-label" text-anchor="middle" style="fill:#0b2418;font-weight:700">${Math.round(
                  v.pct
                )}%</text>`
              : `<text x="${(bx + barWidth / 2).toFixed(1)}" y="${(yTop - 4).toFixed(
                  1
                )}" class="trend-axis-label" text-anchor="middle">${Math.round(v.pct)}%${inProgress ? "↑" : ""}</text>`) +
            `<rect class="trend-bar-hit" x="${bx.toFixed(1)}" y="${paddingTop}" width="${barWidth.toFixed(
              1
            )}" height="${plotHeight}" fill="transparent" data-tooltip="${tip}" data-week="${escapeHtml(
              w.weekStartAt
            )}" data-series="${escapeHtml(v.key)}" data-column="${escapeHtml(column)}"></rect>`
          );
        })
        .join("");
      return bars + label;
    })
    .join("");

  const hatchDefs = visibleSeries
    .map(
      (s) =>
        `<pattern id="perf-hatch-${s.key}" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="${s.color}" fill-opacity="0.12"></rect><line x1="0" y1="0" x2="0" y2="6" stroke="${s.color}" stroke-width="2" stroke-opacity="0.55"></line></pattern>`
    )
    .join("");
  return `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" class="trend-svg"><defs>${hatchDefs}</defs>${gridLines}${groups}</svg>`;
}

// ---- Stats sub-tab: open tickets by created month, stacked by priority ----
const SUPPORT_REPORT_STATS_MONTHS = 6;
const SUPPORT_REPORT_STATS_PRIORITY_COLORS = {
  Urgent: "#f16565",
  High: "#f2994a",
  Medium: "#e5c15c",
  Low: "#6e8bff",
  "(blank)": "#8b93a7",
};

function supportReportStatsHiddenKey(priority) {
  return `stats:${priority}`;
}

// One bucket per month for the last SUPPORT_REPORT_STATS_MONTHS months
// (current month included), plus a leading "Older" bucket for everything
// created before that - counts of currently open Key User tickets by
// priority, scoped to the selected Total / squad radio.
function supportReportStatsBuckets(column) {
  const areas = (supportReportData && supportReportData.areas) || [];
  const scopedAreas = column === "TOTAL" ? areas : areas.filter((a) => a.squad === column);
  const tickets = scopedAreas.flatMap((a) => (a.metrics && a.metrics.openKUTickets) || []);

  const now = new Date();
  const months = [];
  for (let i = SUPPORT_REPORT_STATS_MONTHS - 1; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    months.push({
      index: d.getFullYear() * 12 + d.getMonth(),
      label: d.toLocaleDateString(undefined, { month: "short", year: "2-digit" }),
    });
  }
  const firstIndex = months[0].index;
  const lastIndex = months[months.length - 1].index;
  const buckets = [
    { label: "Older", tooltipLabel: `Created before ${months[0].label}`, counts: {}, monthKey: "older", tickets: [] },
    ...months.map((m) => ({
      label: m.label,
      tooltipLabel: `Created in ${m.label}`,
      counts: {},
      index: m.index,
      monthKey: String(m.index),
      tickets: [],
    })),
  ];

  tickets.forEach((t) => {
    const created = t.createdAt ? new Date(t.createdAt) : null;
    if (!created || Number.isNaN(created.getTime())) return;
    const idx = created.getFullYear() * 12 + created.getMonth();
    const bucket =
      idx < firstIndex ? buckets[0] : buckets[1 + Math.min(idx, lastIndex) - firstIndex];
    const priority = SUPPORT_REPORT_PRIORITY_ORDER.includes(t.priority) ? t.priority : "(blank)";
    bucket.counts[priority] = (bucket.counts[priority] || 0) + 1;
    bucket.tickets.push(t);
  });
  return { buckets, ticketCount: tickets.length };
}

// Stats view mode: null = by created month for the selected Total / squad
// radio; "ASSIGNEE" = one bar per Intercom assignee (all squads), picked via
// the extra "Assignee" radio on the Stats tab only.
const SUPPORT_REPORT_STATS_ASSIGNEE = "ASSIGNEE";
let supportReportStatsView = null;

// One bucket per assignee across every squad's open tickets, biggest first
// (by total across all priorities, so bar order doesn't jump when a priority
// is hidden from the legend).
// Chart shows first names only; "(unassigned)" and "<Team> (team)" stay whole.
function supportReportAssigneeFirstName(name) {
  const text = String(name || "").trim();
  if (!text || text.startsWith("(") || text.endsWith("(team)")) return text;
  return text.split(/\s+/)[0];
}

function supportReportStatsAssigneeBuckets() {
  const areas = (supportReportData && supportReportData.areas) || [];
  const tickets = areas.flatMap((a) => (a.metrics && a.metrics.openKUTickets) || []);
  const byName = new Map();
  tickets.forEach((t) => {
    const name = supportReportFilterLabel(t.assignee);
    if (!byName.has(name)) byName.set(name, { label: supportReportAssigneeFirstName(name), tooltipLabel: supportReportAssigneeFirstName(name), fullName: name, assignee: name, counts: {}, all: 0 });
    const bucket = byName.get(name);
    const priority = SUPPORT_REPORT_PRIORITY_ORDER.includes(t.priority) ? t.priority : "(blank)";
    bucket.counts[priority] = (bucket.counts[priority] || 0) + 1;
    bucket.all += 1;
  });
  const buckets = [...byName.values()].sort((a, b) => b.all - a.all || a.label.localeCompare(b.label));
  return { buckets, ticketCount: tickets.length };
}

// Assignee whose ticket list is expanded under the chart (click a name or
// bar in the by-assignee view; click again to collapse).
let supportReportStatsSelectedAssignee = null;

// Month whose ticket list is expanded under the chart in the by-month view
// (click a bar or its label; click again to collapse). `monthKey` as on
// `supportReportStatsBuckets` buckets ("older" or a month index).
let supportReportStatsSelectedMonth = null;

function supportReportStatsPriorityVisible(t) {
  const priority = SUPPORT_REPORT_PRIORITY_ORDER.includes(t.priority) ? t.priority : "(blank)";
  return !supportReportTrendHiddenSeries.has(supportReportStatsHiddenKey(priority));
}

function supportReportStatsSortTickets(tickets) {
  return tickets.sort(
    (a, b) =>
      SUPPORT_REPORT_PRIORITY_ORDER.indexOf(a.priority) - SUPPORT_REPORT_PRIORITY_ORDER.indexOf(b.priority) ||
      new Date(a.createdAt) - new Date(b.createdAt)
  );
}

function supportReportStatsAssigneeTickets(name) {
  const areas = (supportReportData && supportReportData.areas) || [];
  return supportReportStatsSortTickets(
    areas
      .flatMap((a) => (a.metrics && a.metrics.openKUTickets) || [])
      .filter((t) => supportReportFilterLabel(t.assignee) === name)
      .filter(supportReportStatsPriorityVisible)
  );
}

// The selected month's bucket for the current Total / squad radio, plus its
// tickets (respecting priorities hidden via the legend).
function supportReportStatsMonthSelection() {
  const key = supportReportStatsSelectedMonth;
  if (!key) return null;
  const bucket = supportReportStatsBuckets(supportReportTrendColumn).buckets.find((b) => b.monthKey === key);
  if (!bucket) return null;
  return { bucket, tickets: supportReportStatsSortTickets(bucket.tickets.filter(supportReportStatsPriorityVisible)) };
}

// Ticket table shared by the by-assignee and by-month detail blocks. Columns
// are sortable/filterable via `enhanceSupportReportTables` (the `sf-table` class).
function renderSupportReportStatsTicketsBlock(title, tickets, emptyMessage, showAssignee) {
  const colCount = showAssignee ? 10 : 9;
  const rows = tickets.length
    ? tickets
        .map(
          (t) => `
        <tr>
          <td><a href="${escapeHtml(t.url)}" target="_blank" rel="noopener">${escapeHtml(t.description)}</a></td>
          <td>${escapeHtml(t.squadLabel)}</td>
          <td>${formatDateOnly(t.createdAt)}</td>
          <td>${escapeHtml(t.priority)}</td>
          <td>${escapeHtml(supportReportConversationStatusLabel(t.conversationState))}</td>
          <td>${escapeHtml(supportReportFilterLabel(t.ticketState))}</td>
          <td><span class="status-badge ${slaStatusClass(t.firstResponseSLA)}">${escapeHtml(t.firstResponseSLA)}</span></td>
          <td>${escapeHtml(t.partnerName)}</td>
          <td>${escapeHtml(t.userName)}</td>${
            showAssignee ? `\n          <td>${escapeHtml(supportReportFilterLabel(t.assignee))}</td>` : ""
          }
        </tr>`
        )
        .join("")
    : `<tr><td colspan="${colCount}"><p class="empty-note">${escapeHtml(emptyMessage)}</p></td></tr>`;
  return `
    <div class="squad-block support-stats-detail">
      <h3 class="block-title">${escapeHtml(title)} <span class="label-badge">${tickets.length} open ticket${
    tickets.length === 1 ? "" : "s"
  }</span></h3>
      <table class="data-table filter-table sf-table">
        <thead><tr>
          <th>Ticket</th><th>Squad</th><th>Created</th><th>Priority</th>
          <th class="col-compact">Conversation status</th><th>Ticket status</th>
          <th class="col-compact">First response SLA</th><th>Partner</th><th>User</th>${
            showAssignee ? "<th>Assignee</th>" : ""
          }
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function renderSupportReportStatsDetail() {
  if (supportReportStatsView === SUPPORT_REPORT_STATS_ASSIGNEE) {
    const name = supportReportStatsSelectedAssignee;
    if (!name) return "";
    return renderSupportReportStatsTicketsBlock(
      name,
      supportReportStatsAssigneeTickets(name),
      "No tickets for this assignee with the priorities currently shown.",
      false
    );
  }
  const selection = supportReportStatsMonthSelection();
  if (!selection) return "";
  return renderSupportReportStatsTicketsBlock(
    `${supportReportTrendColumnLabel(supportReportTrendColumn)}: ${selection.bucket.tooltipLabel}`,
    selection.tickets,
    "No open tickets from this month with the priorities currently shown.",
    true
  );
}

function selectSupportReportStatsMonth(key) {
  supportReportStatsSelectedMonth = supportReportStatsSelectedMonth === key ? null : key;
  if (!supportReportData) return;
  renderSupportReport(supportReportData);
  if (supportReportStatsSelectedMonth) {
    const detail = els.supportReportContainer.querySelector(".support-stats-detail");
    if (detail) detail.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function selectSupportReportStatsAssignee(name) {
  supportReportStatsSelectedAssignee = supportReportStatsSelectedAssignee === name ? null : name;
  if (!supportReportData) return;
  renderSupportReport(supportReportData);
  if (supportReportStatsSelectedAssignee) {
    const detail = els.supportReportContainer.querySelector(".support-stats-detail");
    if (detail) detail.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function renderSupportReportStatsSVG(buckets, width, columnLabel, hiddenSeriesKeys, options = {}) {
  // Many bars (one per assignee): slant the x labels so they stay readable.
  const rotate = !!options.rotateLabels;
  const height = rotate ? 280 : 220;
  const paddingLeft = 44;
  const paddingRight = 16;
  const paddingTop = 18;
  const paddingBottom = rotate ? 86 : 26;
  const truncate = (text, max) => (text.length > max ? `${text.slice(0, max - 1)}…` : text);
  const plotWidth = Math.max(1, width - paddingLeft - paddingRight);
  const plotHeight = height - paddingTop - paddingBottom;

  const priorities = SUPPORT_REPORT_PRIORITY_ORDER.filter(
    (p) => !hiddenSeriesKeys || !hiddenSeriesKeys.has(supportReportStatsHiddenKey(p))
  );
  if (!priorities.length) {
    return `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" class="trend-svg">
      <text x="${width / 2}" y="${height / 2}" text-anchor="middle" class="trend-axis-label">Every priority is hidden — click a legend item to show it again.</text>
    </svg>`;
  }
  const totalOf = (b) => priorities.reduce((sum, p) => sum + (b.counts[p] || 0), 0);
  const maxTotal = Math.max(1, ...buckets.map(totalOf));
  const maxValue = maxTotal <= 4 ? 4 : Math.ceil(maxTotal / 4) * 4;
  const yFor = (v) => paddingTop + plotHeight - (v / maxValue) * plotHeight;

  const gridLines = [0, 0.25, 0.5, 0.75, 1]
    .map((t) => {
      const y = paddingTop + plotHeight - t * plotHeight;
      return `<line x1="${paddingLeft}" y1="${y.toFixed(1)}" x2="${width - paddingRight}" y2="${y.toFixed(
        1
      )}" class="trend-gridline" />
        <text x="${paddingLeft - 8}" y="${(y + 4).toFixed(1)}" class="trend-axis-label" text-anchor="end">${Math.round(
        t * maxValue
      )}</text>`;
    })
    .join("");

  const slot = plotWidth / buckets.length;
  const barWidth = Math.max(rotate ? 8 : 14, Math.min(64, slot * 0.6));

  const bars = buckets
    .map((b, i) => {
      const cx = paddingLeft + slot * (i + 0.5);
      const x = cx - barWidth / 2;
      const total = totalOf(b);
      let cumulative = 0;
      const segments = priorities
        .map((p) => {
          const count = b.counts[p] || 0;
          if (!count) return "";
          const yTop = yFor(cumulative + count);
          const yBottom = yFor(cumulative);
          cumulative += count;
          const h = yBottom - yTop;
          const tip = escapeHtml(
            [columnLabel, b.tooltipLabel, `${p}: ${count} of ${total} open ticket${total === 1 ? "" : "s"}`].join("\n")
          );
          const color = SUPPORT_REPORT_STATS_PRIORITY_COLORS[p];
          const label =
            h >= 14
              ? `<text x="${cx.toFixed(1)}" y="${(yTop + h / 2 + 4).toFixed(
                  1
                )}" class="trend-axis-label" text-anchor="middle" style="fill:#0b1218;font-weight:700;pointer-events:none">${count}</text>`
              : "";
          return (
            `<rect x="${x.toFixed(1)}" y="${yTop.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${h.toFixed(
              1
            )}" fill="${color}" opacity="0.85" stroke="var(--surface)" stroke-width="1"></rect>` +
            label +
            `<rect class="trend-stat-hit" x="${x.toFixed(1)}" y="${yTop.toFixed(1)}" width="${barWidth.toFixed(
              1
            )}" height="${h.toFixed(1)}" fill="transparent" style="cursor:pointer" data-tooltip="${tip}"${
              b.assignee ? ` data-assignee="${escapeHtml(b.assignee)}"` : ""
            }${b.monthKey ? ` data-month="${escapeHtml(b.monthKey)}"` : ""}></rect>`
          );
        })
        .join("");
      const totalLabel = total
        ? `<text x="${cx.toFixed(1)}" y="${(yFor(total) - 4).toFixed(
            1
          )}" class="trend-axis-label" text-anchor="middle">${total}</text>`
        : "";
      const selectedClass =
        (b.assignee && b.assignee === options.selected) || (b.monthKey && b.monthKey === options.selectedMonth)
          ? " stats-assignee-selected"
          : "";
      const axisLabel = rotate
        ? `<text x="${cx.toFixed(1)}" y="${(paddingTop + plotHeight + 14).toFixed(
            1
          )}" class="trend-axis-label${b.assignee ? " stats-assignee-label" : ""}${selectedClass}"${
            b.assignee ? ` data-assignee="${escapeHtml(b.assignee)}"` : ""
          } text-anchor="end" transform="rotate(-40 ${cx.toFixed(1)} ${(
            paddingTop +
            plotHeight +
            14
          ).toFixed(1)})">${escapeHtml(truncate(b.label, 18))}</text>`
        : `<text x="${cx.toFixed(1)}" y="${height - 6}" class="trend-axis-label${
            b.monthKey ? " stats-assignee-label" : ""
          }${selectedClass}"${b.monthKey ? ` data-month="${escapeHtml(b.monthKey)}"` : ""} text-anchor="middle">${escapeHtml(
            b.label
          )}</text>`;
      // Full-height click target for the whole bar column (segments for tiny
      // counts are only a pixel or two tall), under the per-priority hits so
      // their tooltips still win on hover. Opens the ticket list below.
      const columnTip = escapeHtml(
        [
          columnLabel,
          b.tooltipLabel,
          ...priorities.filter((p) => b.counts[p]).map((p) => `${p}: ${b.counts[p]}`),
          `Total: ${total} open ticket${total === 1 ? "" : "s"}`,
          "Click to list these tickets",
        ].join("\n")
      );
      const columnHit =
        b.assignee || b.monthKey
          ? `<rect class="trend-stat-hit" x="${(cx - Math.max(barWidth, slot * 0.8) / 2).toFixed(1)}" y="${paddingTop}" width="${Math.max(
              barWidth,
              slot * 0.8
            ).toFixed(1)}" height="${plotHeight.toFixed(1)}" fill="transparent" style="cursor:pointer" data-tooltip="${columnTip}"${
              b.assignee ? ` data-assignee="${escapeHtml(b.assignee)}"` : ""
            }${b.monthKey ? ` data-month="${escapeHtml(b.monthKey)}"` : ""}></rect>`
          : "";
      return columnHit + segments + totalLabel + axisLabel;
    })
    .join("");

  return `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" class="trend-svg" style="height:${height}px">${gridLines}${bars}</svg>`;
}

let supportTrendResizeObserver = null;

// Draws (or redraws, on container resize) the trend chart at the wrap
// div's *actual* pixel width - see `renderSupportReportTrendSVG`'s comment.
function mountSupportReportTrendChart() {
  const wrap = els.supportReportContainer && els.supportReportContainer.querySelector(".trend-svg-wrap");
  const points = supportReportTrendChartPoints();
  const performance = supportReportSubtab === "performance";
  const stats = supportReportSubtab === "stats";
  const weeklyCohorts = (supportReportData && supportReportData.weeklyCohorts) || [];
  if (!wrap || (performance ? !weeklyCohorts.length : stats ? !supportReportData : points.length < 2)) return;
  const draw = () => {
    const width = Math.max(300, Math.round(wrap.clientWidth));
    wrap.innerHTML = stats
      ? supportReportStatsView === SUPPORT_REPORT_STATS_ASSIGNEE
        ? renderSupportReportStatsSVG(
            supportReportStatsAssigneeBuckets().buckets,
            width,
            "Assignee",
            supportReportTrendHiddenSeries,
            { rotateLabels: true, selected: supportReportStatsSelectedAssignee }
          )
        : renderSupportReportStatsSVG(
            supportReportStatsBuckets(supportReportTrendColumn).buckets,
            width,
            supportReportTrendColumnLabel(supportReportTrendColumn),
            supportReportTrendHiddenSeries,
            { selectedMonth: supportReportStatsSelectedMonth }
          )
      : performance
      ? renderSupportReportPerformanceSVG(
          weeklyCohorts,
          width,
          supportReportTrendColumn,
          supportReportTrendColumnLabel(supportReportTrendColumn),
          supportReportTrendHiddenSeries
        )
      : // Open Tickets tab: lines only - weekly bars live on the Performance tab.
        renderSupportReportTrendSVG(
          points,
          width,
          supportReportTrendColumn,
          supportReportTrendColumnLabel(supportReportTrendColumn),
          supportReportTrendHiddenSeries,
          null,
          (supportReportData && supportReportData.dailyEngagement) || []
        );
    attachTrendTooltipHandlers(wrap);
  };
  draw();
  if (supportTrendResizeObserver) supportTrendResizeObserver.disconnect();
  supportTrendResizeObserver = new ResizeObserver(draw);
  supportTrendResizeObserver.observe(wrap);
}

// Which table column (a squad key, or "TOTAL") the trend chart's 5 series
// currently plot - picked via the radio buttons in `renderSupportReportTrendChart`.
let supportReportTrendColumn = "TOTAL";

// Row keys toggled off by clicking their legend item (see
// `renderSupportReportTrendChart`'s legend and the click handler below) -
// persists across column-picker changes/re-renders until toggled back on,
// same session-only lifetime as `supportReportTrendColumn`.
const supportReportTrendHiddenSeries = new Set();

// "Total" plus every squad currently in the main table, in the same order -
// derived from the loaded report rather than hardcoded so it never drifts
// out of sync with `AREAS` in support_report.py.
function supportReportTrendColumnOptions() {
  const areas = (supportReportData && supportReportData.areas) || [];
  return [{ key: "TOTAL", label: "Total" }, ...areas.map((a) => ({ key: a.squad, label: a.label }))];
}

// Display label for a trend column key (e.g. "TOTAL" -> "Total", "PROG" ->
// "Progress") - used to name the selected radio button in each dot's hover
// tooltip (see `renderSupportReportTrendSVG`).
function supportReportTrendColumnLabel(key) {
  const opt = supportReportTrendColumnOptions().find((o) => o.key === key);
  return opt ? opt.label : key;
}

// Clickable to show/hide that row's line - see the click handler on
// `els.supportReportContainer` and `renderSupportReportTrendSVG`'s
// `hiddenSeriesKeys` filtering. Pulled into its own function so a toggle
// click can refresh just the legend's `<div class="trend-legend">` innerHTML
// (via `refreshSupportReportTrendLegendAndChart`) without re-rendering the
// whole tab.
function renderSupportReportTrendLegend() {
  const lineLegend = SUPPORT_REPORT_ROWS.map((row, idx) => {
    const hidden = supportReportTrendHiddenSeries.has(row.key);
    return `<span class="trend-legend-item${
      hidden ? " trend-legend-item-hidden" : ""
    }" data-series-key="${escapeHtml(row.key)}" title="Click to ${
      hidden ? "show" : "hide"
    } this line" role="button"><span class="trend-legend-swatch" style="background:${
      SUPPORT_REPORT_TREND_COLORS[idx % SUPPORT_REPORT_TREND_COLORS.length]
    }"></span>${escapeHtml(row.label)}</span>`;
  }).join("");
  const barLegend = SUPPORT_REPORT_WEEKLY_BAR_SERIES.map((row, idx) => {
    const hidden = supportReportTrendHiddenSeries.has(row.key);
    return `<span class="trend-legend-item trend-legend-item-bar${
      hidden ? " trend-legend-item-hidden" : ""
    }" data-series-key="${escapeHtml(row.key)}" title="Click to ${
      hidden ? "show" : "hide"
    } this bar series" role="button"><span class="trend-legend-swatch trend-legend-swatch-bar" style="background:${
      SUPPORT_REPORT_WEEKLY_BAR_COLORS[idx]
    }"></span>${escapeHtml(row.label)}</span>`;
  }).join("");
  const responsesHidden = supportReportTrendHiddenSeries.has(SUPPORT_REPORT_RESPONSES_SERIES.key);
  const responsesLegend = `<span class="trend-legend-item${
    responsesHidden ? " trend-legend-item-hidden" : ""
  }" data-series-key="${escapeHtml(SUPPORT_REPORT_RESPONSES_SERIES.key)}" title="Click to ${
    responsesHidden ? "show" : "hide"
  } this line" role="button"><span class="trend-legend-swatch" style="background:${
    SUPPORT_REPORT_RESPONSES_COLOR
  }"></span>${escapeHtml(SUPPORT_REPORT_RESPONSES_SERIES.label)}</span>`;
  const priorityLegend = SUPPORT_REPORT_PRIORITY_ORDER.map((p) => {
    const hidden = supportReportTrendHiddenSeries.has(supportReportStatsHiddenKey(p));
    return `<span class="trend-legend-item${
      hidden ? " trend-legend-item-hidden" : ""
    }" data-series-key="${escapeHtml(supportReportStatsHiddenKey(p))}" title="Click to ${
      hidden ? "show" : "hide"
    } this priority" role="button"><span class="trend-legend-swatch" style="background:${
      SUPPORT_REPORT_STATS_PRIORITY_COLORS[p]
    }"></span>${escapeHtml(p)}</span>`;
  }).join("");
  if (supportReportSubtab === "stats") return priorityLegend;
  return supportReportSubtab === "performance" ? barLegend : lineLegend + responsesLegend;
}

function renderSupportReportColumnPicker() {
  // The Stats tab adds an extra "Assignee" option (one bar per assignee).
  const inStats = supportReportSubtab === "stats";
  const assigneeOn = inStats && supportReportStatsView === SUPPORT_REPORT_STATS_ASSIGNEE;
  const options = supportReportTrendColumnOptions();
  if (inStats) options.push({ key: SUPPORT_REPORT_STATS_ASSIGNEE, label: "Assignee" });
  return options
    .map((opt) => {
      const checked = assigneeOn ? opt.key === SUPPORT_REPORT_STATS_ASSIGNEE : opt.key === supportReportTrendColumn;
      return `
      <label class="trend-column-option">
        <input type="radio" name="trend-column" value="${escapeHtml(opt.key)}"${checked ? " checked" : ""}>
        ${escapeHtml(opt.label)}
      </label>`;
    })
    .join("");
}

// Stats-tab radio change: "Assignee" switches the chart to one bar per
// assignee; a Total / squad option goes back to the by-month view for it.
function selectSupportReportStatsView(value) {
  if (value === SUPPORT_REPORT_STATS_ASSIGNEE) {
    supportReportStatsView = SUPPORT_REPORT_STATS_ASSIGNEE;
  } else {
    supportReportStatsView = null;
    if (value && value !== supportReportTrendColumn) {
      supportReportTrendColumn = value;
      syncTrendColumnSquadFilter();
    }
  }
  if (supportReportData) renderSupportReport(supportReportData);
}

// Performance sub-tab: weekly SLA % bars only. Clicking a bar fills the debug
// table below (see `renderSupportReportDebug`).
function renderSupportReportPerformanceChart() {
  const weeklyCohorts = (supportReportData && supportReportData.weeklyCohorts) || [];
  if (!weeklyCohorts.length) {
    return `
      <div class="squad-block support-trend-chart">
        <h3 class="block-title">Weekly SLA performance</h3>
        <p class="empty-note">No weekly cohort data yet — it's computed each time the report refreshes.</p>
      </div>`;
  }
  return `
    <div class="squad-block support-trend-chart">
      <h3 class="block-title">Weekly SLA performance <span class="label-badge">Last ${weeklyCohorts.length} week${
    weeklyCohorts.length === 1 ? "" : "s"
  }</span></h3>
      <div class="trend-controls">
        <div class="trend-column-picker">${renderSupportReportColumnPicker()}</div>
      </div>
      <div class="trend-svg-wrap"></div>
      <div class="trend-legend">${renderSupportReportTrendLegend()}</div>
      <p class="empty-note trend-cohort-note">
        Last 6 Pacific calendar weeks (tickets created that week). Green = % Urgent/High that met resolution SLA; blue = % of that week's tickets (any state) whose first response came within the SLA window (tickets still awaiting a reply inside the window are excluded). For the green bar, every Urgent/High ticket is in the denominator, so recent weeks start low: the solid part is the share already met, and the hatched part (↑) is open tickets still inside the 21-day window that could still be met. Click a bar to see the tickets behind it.
      </p>
    </div>`;
}

// Stats sub-tab: histogram of currently open tickets by created month,
// each bar stacked by priority.
function renderSupportReportStatsChart() {
  const byAssignee = supportReportStatsView === SUPPORT_REPORT_STATS_ASSIGNEE;
  const { ticketCount } = byAssignee
    ? supportReportStatsAssigneeBuckets()
    : supportReportStatsBuckets(supportReportTrendColumn);
  const note = byAssignee
    ? `Currently open Key User tickets (Intercom state open or snoozed) across all squads, one bar per Intercom
        assignee (a team name with "(team)" when it's assigned to a team and not a person), biggest first and
        stacked by priority. The number on top of each bar is its total. Click a name or a bar to list that person's tickets below; click a legend item to hide a priority.`
    : `Currently open Key User tickets (Intercom state open or snoozed), grouped by the month they were created
        (last ${SUPPORT_REPORT_STATS_MONTHS} months; everything earlier is in "Older") and stacked by priority.
        The number on top of each bar is its total. Click a month's bar or label to list that month's tickets below; click a legend item to hide a priority.`;
  return `
    <div class="squad-block support-trend-chart">
      <h3 class="block-title">${byAssignee ? "Open tickets by assignee" : "Open tickets by created month"} <span class="label-badge">${ticketCount} open ticket${
    ticketCount === 1 ? "" : "s"
  }</span></h3>
      <div class="trend-controls">
        <div class="trend-column-picker">${renderSupportReportColumnPicker()}</div>
      </div>
      <div class="trend-svg-wrap"></div>
      <div class="trend-legend">${renderSupportReportTrendLegend()}</div>
      <p class="empty-note trend-cohort-note">
        ${note}
      </p>
    </div>`;
}

function renderSupportReportTrendChart() {
  if (supportReportSubtab === "stats") return renderSupportReportStatsChart();
  if (supportReportSubtab === "performance") return renderSupportReportPerformanceChart();
  const points = supportReportTrendChartPoints();
  const totalStored =
    (supportReportHistoryData && supportReportHistoryData.totalPointsStored) || points.length;
  if (points.length < 2) {
    return `
      <div class="squad-block support-trend-chart">
        <h3 class="block-title">Trend</h3>
        <p class="empty-note">
          Not enough history yet to chart a trend — one point is logged every time this report actually refreshes
          (once a day at most, or whenever someone hits Update), not on every page view. Check back after a couple
          of refreshes.
        </p>
      </div>`;
  }
  const legend = renderSupportReportTrendLegend();
  const columnPicker = renderSupportReportColumnPicker();
  return `
    <div class="squad-block support-trend-chart">
      <h3 class="block-title">Trend <span class="label-badge">Last ${points.length} day${
    points.length === 1 ? "" : "s"
  }${
    totalStored > points.length ? ` (${totalStored} stored)` : ""
  }</span></h3>
      <div class="trend-controls">
        <div class="trend-column-picker">${columnPicker}</div>
      </div>
      <div class="trend-svg-wrap"></div>
      <div class="trend-legend">${legend}</div>
      <p class="empty-note trend-cohort-note">
        Last ${SUPPORT_REPORT_TREND_CHART_MAX_POINTS} days, one point per day (the day's latest refresh). Weekly SLA bars are on the Performance tab.
        The orange line has one point per snapshot, showing the number of customer-facing replies Stellic sent on Key User tickets during that snapshot's Pacific calendar day (the latest day is partial) — counted by the day the reply was sent, on any Key User ticket whether or not it closed (human teammates only; internal notes and bot replies excluded). Click a dot to see which tickets were responded to.${
          supportReportData && supportReportData.engagement && supportReportData.engagement.complete === false
            ? ` <strong>Still filling in:</strong> ${supportReportData.engagement.ticketsNotYetFetched} ticket(s) haven't been scanned yet, so recent counts may be low until the next refresh.`
            : ""
        }
      </p>
    </div>`;
}

function renderSupportReport(data) {
  if (!els.supportReportContainer) return;
  closeAllSupportReportMultiSelectMenus();
  closeSupportReportDateMenu();
  supportReportData = data;
  const areas = data.areas || [];

  const colHighlight = (key) => (key === supportReportTrendColumn ? " support-col-highlight" : "");
  const headerCells =
    `<th class="support-squad-col support-total-col${colHighlight("TOTAL")}" data-col-key="TOTAL">Total</th>` +
    areas
      .map(
        (area) =>
          `<th class="support-squad-col${colHighlight(area.squad)}" data-col-key="${escapeHtml(
            area.squad
          )}">${escapeHtml(area.label)}</th>`
      )
      .join("");
  const bodyRows = SUPPORT_REPORT_ROWS.map((row) => {
    const values = areas.map((area) => (area.metrics ? area.metrics[row.key] : null));
    const total = values.reduce((sum, v) => sum + (typeof v === "number" ? v : 0), 0);
    const cells =
      `<td class="num support-squad-col support-total-col${colHighlight("TOTAL")}" data-col-key="TOTAL">${total}</td>` +
      values
        .map(
          (value, idx) =>
            `<td class="num support-squad-col${colHighlight(areas[idx].squad)}" data-col-key="${escapeHtml(
              areas[idx].squad
            )}">${value === null || value === undefined ? "—" : value}</td>`
        )
        .join("");
    const activeClass = row.key === supportReportActiveMetric ? " active-row" : "";
    return `<tr class="clickable-row${activeClass}" data-metric="${row.key}"><td>${escapeHtml(
      row.label
    )}</td>${cells}</tr>`;
  }).join("");

  const subtabBar = `
    <div class="support-subtabs" role="tablist">${SUPPORT_REPORT_SUBTABS.map(
      (t) =>
        `<button type="button" role="tab" class="support-subtab-btn${
          t.key === supportReportSubtab ? " active" : ""
        }" data-support-subtab="${t.key}">${escapeHtml(t.label)}</button>`
    ).join("")}</div>`;

  if (supportReportSubtab === "stats") {
    els.supportReportContainer.innerHTML = `
    ${subtabBar}
    ${renderSupportReportTrendChart()}
    ${renderSupportReportStatsDetail()}`;
    mountSupportReportTrendChart();
    enhanceSupportReportTables();
    updateSupportReportUpdatedAt(data);
    return;
  }

  if (supportReportSubtab === "performance") {
    els.supportReportContainer.innerHTML = `
    ${subtabBar}
    ${renderSupportReportTrendChart()}
    ${renderSupportReportDebug()}`;
    mountSupportReportTrendChart();
    enhanceSupportReportTables();
    updateSupportReportUpdatedAt(data);
    return;
  }

  els.supportReportContainer.innerHTML = `
    ${subtabBar}
    ${renderSupportReportTrendChart()}
    ${renderSupportReportDebug({ responsesOnly: true })}
    <div class="squad-block">
      <p class="quality-definitions" style="list-style: none; padding-left: 0;">
        Key User tickets only. "Open" means Intercom state open or snoozed. First response SLA is
        ${data.frTargetHours} business hours (weekends don't count); resolution SLA is ${data.resTargetDays}
        calendar days for Urgent/High priority tickets. "This week" is week-to-date (resets every Monday,
        Pacific time)${
          data.weekStartAt
            ? ` - currently counting since ${new Date(data.weekStartAt).toLocaleDateString(undefined, {
                month: "short",
                day: "numeric",
              })}`
            : ""
        }. Click a row to see the underlying tickets.
      </p>
      <table class="data-table support-report-table">
        <thead><tr><th></th>${headerCells}</tr></thead>
        <tbody>${bodyRows}</tbody>
      </table>
    </div>
    ${renderSupportReportDrilldown()}`;

  mountSupportReportTrendChart();
  enhanceSupportReportTables();
  updateSupportReportUpdatedAt(data);
}

function updateSupportReportUpdatedAt(data) {
  if (els.supportReportUpdatedAt && data.fetchedAt) {
    const asOfSuffix = data.asOf ? ` (as of ${new Date(data.asOf).toLocaleString()})` : "";
    els.supportReportUpdatedAt.textContent = `Updated ${formatRelativeTime(data.fetchedAt)}${asOfSuffix}`;
    els.supportReportUpdatedAt.classList.toggle("stale", isStale(data.fetchedAt));
    els.supportReportUpdatedAt.title = new Date(data.fetchedAt * 1000).toLocaleString();
  }
}

if (els.supportReportContainer) {
  els.supportReportContainer.addEventListener("click", (event) => {
    const subtabBtn = event.target.closest(".support-subtab-btn");
    if (subtabBtn) {
      const next = subtabBtn.dataset.supportSubtab;
      if (next && next !== supportReportSubtab && supportReportData) {
        supportReportSubtab = next;
        renderSupportReport(supportReportData);
      }
      return;
    }

    const sortHeader = event.target.closest(".support-drilldown th[data-sort-key]");
    if (sortHeader) {
      toggleSupportReportSort(sortHeader.dataset.sortKey);
      return;
    }

    const assigneeTarget = event.target.closest("[data-assignee]");
    if (assigneeTarget && supportReportSubtab === "stats") {
      selectSupportReportStatsAssignee(assigneeTarget.dataset.assignee);
      return;
    }

    const monthTarget = event.target.closest("[data-month]");
    if (monthTarget && supportReportSubtab === "stats") {
      selectSupportReportStatsMonth(monthTarget.dataset.month);
      return;
    }

    const barHit = event.target.closest(".trend-bar-hit");
    if (barHit) {
      selectSupportReportDebug({
        weekStartAt: barHit.dataset.week,
        seriesKey: barHit.dataset.series,
        column: barHit.dataset.column,
      });
      return;
    }

    const multiSelectTrigger = event.target.closest(".multi-select-trigger");
    if (multiSelectTrigger) {
      const wrap = multiSelectTrigger.closest(".multi-select-filter");
      if (!wrap) return;
      openSupportReportMultiSelectMenu(wrap);
      event.stopPropagation();
      return;
    }

    closeAllSupportReportMultiSelectMenus();

    const legendItem = event.target.closest(".trend-legend-item");
    if (legendItem) {
      const key = legendItem.dataset.seriesKey;
      if (supportReportTrendHiddenSeries.has(key)) {
        supportReportTrendHiddenSeries.delete(key);
      } else {
        supportReportTrendHiddenSeries.add(key);
      }
      refreshSupportReportTrendLegendAndChart();
      return;
    }

    const colHeader = event.target.closest(".support-report-table thead th[data-col-key]");
    if (colHeader) {
      selectSupportReportTrendColumn(colHeader.dataset.colKey);
      return;
    }

    const row = event.target.closest("tr.clickable-row");
    if (!row || !supportReportData) return;
    const metric = row.dataset.metric;
    supportReportActiveMetric = supportReportActiveMetric === metric ? null : metric;
    renderSupportReport(supportReportData);
  });

  els.supportReportContainer.addEventListener("input", (event) => {
    const filterKey = event.target.dataset.filter;
    if (!filterKey || event.target.tagName !== "INPUT" || event.target.type === "checkbox") return;
    supportReportFilters[filterKey] = event.target.value;
    updateSupportReportDrilldownRows();
  });

  els.supportReportContainer.addEventListener("change", (event) => {
    if (event.target.name === "trend-column") {
      if (supportReportSubtab === "stats") {
        selectSupportReportStatsView(event.target.value);
      } else {
        selectSupportReportTrendColumn(event.target.value);
      }
      return;
    }
    const filterKey = event.target.dataset.filter;
    if (!filterKey || event.target.tagName !== "SELECT") return;
    supportReportFilters[filterKey] = event.target.value;
    updateSupportReportDrilldownRows();
  });

  document.addEventListener("click", (event) => {
    if (event.target.closest(".date-range-picker-menu") || event.target.closest(".date-range-picker-trigger")) {
      if (event.target.closest(".date-range-picker-trigger")) {
        openSupportReportDateMenu(event.target.closest(".date-range-picker-trigger"));
      } else if (supportReportDateMenuEl) {
        handleSupportReportDateMenuClick(event);
      }
      return;
    }
    closeSupportReportDateMenu();
    if (event.target.closest(".multi-select-menu") || event.target.closest(".multi-select-trigger")) return;
    closeAllSupportReportMultiSelectMenus();
  });

  document.addEventListener("change", (event) => {
    if (event.target.type !== "checkbox") return;
    if (!event.target.closest(".multi-select-menu")) return;
    handleSupportReportMultiSelectChange(event.target);
  });
}

let supportReportLoaded = false;

// A cache-version bump (or the daily 24h expiry) makes the *first* hit of
// the day block for ~1-2 minutes with nothing sent back to the browser
// while it pulls fresh data from Intercom - long enough that some browsers/
// networks give up on the connection and `fetch()` throws a plain
// "Failed to fetch" TypeError (a network-level failure, not an HTTP error,
// so a `!res.ok` check never sees it) even though the server keeps working
// and finishes writing the cache regardless. Retrying after a delay picks
// up that now-warm cache instead of surfacing a scary error for what's
// really just a slow-but-successful first load. Delays are long enough to
// clear that worst case rather than piling up duplicate concurrent pulls.
const SUPPORT_REPORT_RETRY_DELAYS_MS = [20000, 45000];

function isNetworkFetchError(err) {
  return err instanceof TypeError;
}

// Best-effort - a history-log request failing shouldn't take down the main
// report, and doesn't need the same retry treatment (it's a fast, cheap
// read regardless of whether the main report is cold).
async function loadSupportReportHistory() {
  try {
    const res = await fetch("/api/support-report/history");
    if (!res.ok) return;
    supportReportHistoryData = await res.json();
  } catch (err) {
    // swallow - trend chart just won't render this time.
  }
}

async function loadSupportReport() {
  if (!els.supportReportContainer) return;
  for (let attempt = 0; ; attempt++) {
    try {
      const res = await fetch("/api/support-report");
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Request failed (${res.status})`);
      }
      const data = await res.json();
      await loadSupportReportHistory();
      renderSupportReport(data);
      return;
    } catch (err) {
      const canRetry = isNetworkFetchError(err) && attempt < SUPPORT_REPORT_RETRY_DELAYS_MS.length;
      if (!canRetry) {
        els.supportReportContainer.innerHTML = `<p class="empty-note">Couldn't load the support report: ${escapeHtml(
          err.message
        )}</p>`;
        return;
      }
      els.supportReportContainer.innerHTML =
        '<p class="empty-note">Still working — the first load of the day can take up to ~2 minutes while it pulls fresh data from Intercom. Retrying…</p>';
      await new Promise((resolve) => setTimeout(resolve, SUPPORT_REPORT_RETRY_DELAYS_MS[attempt]));
    }
  }
}

async function refreshSupportReport() {
  const btn = els.supportReportUpdateBtn;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span><span class="btn-label">Updating… (~1-2 min)</span>';
  }
  try {
    const res = await fetch("/api/support-report/refresh", { method: "POST" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    const data = await res.json();
    // The refresh just logged a new history point server-side - re-fetch so
    // the trend chart picks it up rather than showing last load's data.
    await loadSupportReportHistory();
    renderSupportReport(data);
  } catch (err) {
    showError(`Couldn't update the support report: ${err.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<span class="btn-label">Update</span>';
    }
  }
}

if (els.supportReportUpdateBtn) {
  els.supportReportUpdateBtn.addEventListener("click", refreshSupportReport);
}

// ---- Partner Insights ----
// Allowlist-gated tab (see `loadCurrentUser`/`partnerInsightsAccess`) - the
// backend is the real gate (403s the API routes for anyone not on
// PARTNER_INSIGHTS_ALLOWED_EMAILS), this just keeps the tab out of sight
// for everyone else. Mirrors the Support Report tab's structure closely
// (see `renderSupportReport`/`renderSupportReportDrilldown` above).

let partnerInsightsData = null;
let partnerInsightsActivePartnerId = null;
// Which single partner's per-row Update button (see `updateSinglePartner`)
// is currently in flight, if any - `null` the rest of the time. Only one
// at a time (a second click while one's in flight is a no-op) to keep the
// UI simple; the whole-roster Update button above the table is unaffected
// and can still run concurrently with this.
let partnerInsightsUpdatingId = null;
// Which escalation finding rows are expanded in-place (see
// `renderEscalationsBlock`) - keyed `${partnerId}:${index}` rather than
// just index, so switching to a different partner's expanded row doesn't
// carry over some other partner's expanded finding by coincidence of
// index. A Set (not a single value) so more than one finding can be open
// at once, unlike the single-active-partner row above.
const partnerInsightsExpandedEscalations = new Set();

function scoreBand(score) {
  if (score === null || score === undefined) return "";
  if (score >= 85) return "score-ok"; // green
  if (score < 60) return "score-over"; // red
  return "score-warn"; // yellow
}

// A plain red/yellow/green dot - the exact number is still available on
// hover (title attribute) for anyone who wants it, but the at-a-glance
// table reads as a traffic light rather than a wall of numbers.
function renderScoreCell(score, emptyLabel) {
  if (score === null || score === undefined) {
    return `<span class="empty-note-inline">${escapeHtml(emptyLabel)}</span>`;
  }
  return `<span class="partner-score-dot ${scoreBand(score)}" title="Score: ${score}/100"></span>`;
}

// Escalation severity badges - a small colored pill (not a dot, unlike the
// score columns above) since the label itself ("LIVE FIRE") carries
// meaning that's worth showing at a glance, not just hidden in a tooltip.
const ESCALATION_SEVERITY_CLASS = {
  LIVE_FIRE: "escalation-badge-live-fire",
  SMOLDERING: "escalation-badge-smoldering",
  WATCH: "escalation-badge-watch",
};
const ESCALATION_SEVERITY_LABEL = {
  LIVE_FIRE: "Live fire",
  SMOLDERING: "Smoldering",
  WATCH: "Watch",
};

function renderEscalationBadge(severity) {
  if (!severity) return "";
  return `<span class="escalation-badge ${ESCALATION_SEVERITY_CLASS[severity] || ""}">${escapeHtml(
    ESCALATION_SEVERITY_LABEL[severity] || severity
  )}</span>`;
}

// Shared by the main-table Live Fire/Smoldering/Watch columns and their
// sort values - how many of a partner's currently-tracked items are at
// exactly this severity. `null` (not 0) when there's no escalation data
// at all for this partner, so "not in Vitally"/"not configured" can
// sort/render distinctly from a genuine zero.
function escalationSeverityCount(escalations, severity) {
  if (!escalations) return null;
  return (escalations.items || []).filter((item) => item.severity === severity).length;
}

// Main-table cell for one severity's count (Live Fire / Smoldering / Watch)
// - a quiet "-" for a genuine zero, or "not in Vitally"/"not configured"
// when there's no escalation data at all for this partner.
function escalationsUnavailableText(escalationsConfigured, escalationsError) {
  if (!escalationsConfigured) {
    return "Escalations aren't configured yet (set ESCALATION_AGENT_URL) - see README.";
  }
  if (escalationsError) return "Escalation agent unavailable - try again shortly.";
  return "Not matched to a Vitally account - no partner emails to triage.";
}

function renderEscalationCountCell(escalations, escalationsConfigured, severity, escalationsError) {
  const count = escalationSeverityCount(escalations, severity);
  if (count === null) {
    const label = !escalationsConfigured ? "not configured" : escalationsError ? "unavailable" : "not in Vitally";
    return `<span class="empty-note-inline">${label}</span>`;
  }
  if (!count) {
    return `<span class="empty-note-inline">-</span>`;
  }
  return `<span class="escalation-count-badge ${ESCALATION_SEVERITY_CLASS[severity] || ""}">${count}</span>`;
}

// Computed live from `lastMovementAt` on every render rather than a number
// the LLM wrote once - stays accurate between refreshes without needing a
// new LLM call.
function daysSince(isoDate) {
  if (!isoDate) return null;
  const then = new Date(isoDate).getTime();
  if (Number.isNaN(then)) return null;
  return Math.max(0, Math.floor((Date.now() - then) / 86400000));
}

const BLOCKED_ON_LABEL = { us: "Us", them: "Them", unclear: "Unclear" };

// The expanded row's Escalations block: a findings table where each row
// expands in-place to its full detail (evidence, blocked-on reason, etc.)
// on click - same "row expands into the row right below it" pattern as
// the outer partner table itself, rather than a separate summary table
// plus a fully-separate list of detail cards repeating the same items -
// and, unlike a one-off ad-hoc check, a "Recent emails" section sourced
// from `escalations.recentEmails` (the escalation agent's latest batch) so
// the raw source material stays visible between updates.
function renderEscalationsBlock(partner, escalationsConfigured, escalationsError) {
  const escalations = partner.escalations;
  if (!escalations) {
    return `<p class="empty-note">${escapeHtml(escalationsUnavailableText(escalationsConfigured, escalationsError))}</p>`;
  }
  const items = (escalations.items || [])
    .slice()
    .sort((a, b) => (ESCALATION_SEVERITY_RANK[b.severity] || 0) - (ESCALATION_SEVERITY_RANK[a.severity] || 0));
  const linkSuffix = escalations.vitallyAccountUrl
    ? ` <a href="${escapeHtml(
        escalations.vitallyAccountUrl
      )}" target="_blank" rel="noopener" class="count-link">Open account in Vitally</a>`
    : "";

  let findingsHtml;
  if (!items.length) {
    findingsHtml = `<p class="empty-note">No live or brewing escalations found in this partner's recent email${
      escalations.checkedAt ? ` - last checked ${formatRelativeTime(new Date(escalations.checkedAt).getTime() / 1000)}` : ""
    }.</p>`;
  } else {
    const rows = items
      .map((item, index) => {
        const days = daysSince(item.lastMovementAt);
        // Keyed by partner + index (not just index) so a different
        // partner's expanded row never coincidentally inherits this one's
        // expand state - see `partnerInsightsExpandedEscalations`.
        const key = `${partner.partnerId}:${index}`;
        const isOpen = partnerInsightsExpandedEscalations.has(key);
        const summaryRow = `
      <tr class="clickable-row escalation-finding-row${isOpen ? " active-row" : ""}" data-escalation-key="${escapeHtml(
          key
        )}">
        <td>${renderEscalationBadge(item.severity)}</td>
        <td>${escapeHtml(item.headline)}</td>
        <td>${escapeHtml(BLOCKED_ON_LABEL[item.blockedOn] || item.blockedOn)}</td>
        <td class="num">${days === null ? "—" : `${days}d ago`}</td>
      </tr>`;
        if (!isOpen) return summaryRow;

        const evidenceRows = (item.evidence || [])
          .map(
            (e) =>
              `<li>"${escapeHtml(e.quote)}" — ${escapeHtml(e.sender || "")}${
                e.date ? `, ${escapeHtml(formatDateTime(e.date))}` : ""
              }</li>`
          )
          .join("");
        const detailRow = `
      <tr class="escalation-finding-detail-row">
        <td colspan="4">
          <p class="escalation-card-reason">${escapeHtml(item.severityReason || "")}</p>
          ${evidenceRows ? `<ul class="escalation-evidence">${evidenceRows}</ul>` : ""}
          <table class="data-table" style="margin-top: 8px;">
            <tbody>
              <tr><td>Blocked on</td><td>${escapeHtml(BLOCKED_ON_LABEL[item.blockedOn] || item.blockedOn)}${
          item.blockedOnReason ? ` — ${escapeHtml(item.blockedOnReason)}` : ""
        }</td></tr>
              <tr><td>Days since last movement</td><td class="num">${days === null ? "—" : days}</td></tr>
              <tr><td>From</td><td>${escapeHtml(item.from || "")}</td></tr>
              <tr><td>Subject</td><td>${escapeHtml(item.subject || "")}</td></tr>
              <tr><td>Last email</td><td>${item.lastEmailDate ? escapeHtml(formatDateTime(item.lastEmailDate)) : "—"}</td></tr>
            </tbody>
          </table>
        </td>
      </tr>`;
        return summaryRow + detailRow;
      })
      .join("");
    findingsHtml = `
      <table class="data-table escalation-findings-table" style="margin-bottom: 12px;">
        <thead>
          <tr><th>Severity</th><th>Headline</th><th>Blocked on</th><th class="num">Last movement</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>${linkSuffix}`;
  }

  const emails = (escalations.recentEmails || [])
    .slice()
    .sort((a, b) => (b.date || "").localeCompare(a.date || ""));
  const emailsHtml = !emails.length
    ? ""
    : `
      <h4 class="block-subtitle" style="margin-top: 16px;">Recent emails analyzed (${emails.length})</h4>
      <div class="escalation-emails">
        ${emails
          .map(
            (e) => `
        <details class="escalation-email-item">
          <summary>
            <span class="escalation-email-date">${escapeHtml(formatDateTime(e.date))}</span>
            <span class="escalation-email-from">${escapeHtml(e.from || "")}</span>
            <span class="escalation-email-subject">${escapeHtml(e.subject || "")}</span>
          </summary>
          <div class="escalation-email-body">${escapeHtml(e.body || "")}</div>
        </details>`
          )
          .join("")}
      </div>`;

  return findingsHtml + emailsHtml;
}

const PARTNER_INSIGHTS_COLUMNS = 6; // Partner, Bug Score, Live Fire, Smoldering, Watch, Update
// Feature Score is hidden from this main table (still shown in the
// expanded row's Product breakdown below) - kept out of
// `PARTNER_INSIGHTS_SORT_COLUMNS`/`partnerInsightsSortValue` entirely
// rather than just skipped when rendering, so there's no dead sort state
// a user could get stuck on.

// Column headers are clickable to sort - see `renderPartnerInsights`'s
// `<th data-sort-key>` and the click handler below. Defaults to Partner
// name, ascending.
const partnerInsightsSort = { key: "name", dir: "asc" };

const PARTNER_INSIGHTS_SORT_COLUMNS = [
  { key: "name", label: "Partner" },
  { key: "bugScore", label: "Bug Score" },
  { key: "liveFireCount", label: "Live Fire" },
  { key: "smolderingCount", label: "Smoldering" },
  { key: "watchCount", label: "Watch" },
];

// Higher = more urgent - used to order items within the expanded row
// (most urgent first), matching the triage prompt's "Rank by urgency, not
// by date" instruction.
const ESCALATION_SEVERITY_RANK = { LIVE_FIRE: 3, SMOLDERING: 2, WATCH: 1 };

function partnerInsightsSortValue(partner, key) {
  if (key === "name") return partner.name || "";
  if (key === "bugScore") return partner.product ? partner.product.bugScore : null;
  if (key === "liveFireCount") return escalationSeverityCount(partner.escalations, "LIVE_FIRE");
  if (key === "smolderingCount") return escalationSeverityCount(partner.escalations, "SMOLDERING");
  if (key === "watchCount") return escalationSeverityCount(partner.escalations, "WATCH");
  return null;
}

function partnerInsightsRows() {
  const rows = ((partnerInsightsData && partnerInsightsData.partners) || []).slice();
  const { key, dir } = partnerInsightsSort;
  const mult = dir === "asc" ? 1 : -1;
  rows.sort((a, b) => {
    const va = partnerInsightsSortValue(a, key);
    const vb = partnerInsightsSortValue(b, key);
    // Partners missing that particular score ("not linked"/"no data yet")
    // always sort to the bottom regardless of direction, rather than
    // flip-flopping to the top on a descending sort.
    if (va === null && vb === null) return 0;
    if (va === null) return 1;
    if (vb === null) return -1;
    if (typeof va === "string") return va.localeCompare(vb) * mult;
    return (va - vb) * mult;
  });
  return rows;
}

// Renders a raw count as a link to the exact matching Linear issues
// (`_multi_issue_url` server-side - an ad-hoc "/issues/ID-1,ID-2,..." list
// view) whenever there's a non-empty, non-zero bucket to show; otherwise
// just the plain number (0, or no workspace URL could be sniffed).
function renderCountLink(count, url) {
  if (!url || !count) return String(count);
  return `<a class="count-link" href="${escapeHtml(url)}" target="_blank" rel="noopener" title="Open these ${count} issue(s) in Linear">${count}</a>`;
}

// The expanded-row content for one partner - rendered as a single wide
// `<td colspan>` directly under that partner's own row in the main table
// (see `renderPartnerInsights`), rather than one shared panel pinned to
// the bottom of the whole table, so it's obvious which partner it belongs
// to and other rows don't shift around unexpectedly.
function renderPartnerInsightsExpandedRow(partner) {
  const product = partner.product;

  const productBlock = !product
    ? `<p class="empty-note">Not linked to a Linear customer yet - no Product scores available.</p>`
    : `
      <table class="data-table">
        <tbody>
          <tr><td>Bug score</td><td class="num">${renderScoreCell(product.bugScore, "n/a")}</td></tr>
          <tr><td>Bugs (total / new this month)</td><td class="num">${renderCountLink(product.totalBugs, product.totalBugsUrl)} / ${renderCountLink(product.newBugsThisMonth, product.newBugsThisMonthUrl)}</td></tr>
          <tr><td>Currently out of SLA</td><td class="num">${renderCountLink(product.bugsCurrentlyOutOfSla, product.bugsCurrentlyOutOfSlaUrl)}</td></tr>
          <tr><td>Failed SLA this month</td><td class="num">${renderCountLink(product.bugsFailedSlaThisMonth, product.bugsFailedSlaThisMonthUrl)}</td></tr>
          <tr><td>SLA-eligible bugs (Urgent/High)</td><td class="num">${renderCountLink(product.slaEligibleBugs, product.slaEligibleBugsUrl)}</td></tr>
          <tr><td>Feature score</td><td class="num">${renderScoreCell(product.featureScore, "n/a")}</td></tr>
          <tr><td>Feature requests/other (total / new this month)</td><td class="num">${renderCountLink(product.totalFeatureRequests, product.totalFeatureRequestsUrl)} / ${renderCountLink(product.newFeatureRequestsThisMonth, product.newFeatureRequestsThisMonthUrl)}</td></tr>
          <tr><td>Stale (open &gt;90d, unresolved)</td><td class="num">${renderCountLink(product.staleFeatureRequests, product.staleFeatureRequestsUrl)}</td></tr>
        </tbody>
      </table>`;

  return `
    <tr class="partner-insights-expanded-row">
      <td colspan="${PARTNER_INSIGHTS_COLUMNS}">
        <div>
          <h4 class="block-subtitle">Product</h4>
          ${productBlock}
        </div>
        <div style="margin-top: 16px;">
          <h4 class="block-subtitle">Escalations</h4>
          ${renderEscalationsBlock(
            partner,
            partnerInsightsData && partnerInsightsData.escalationsConfigured,
            partnerInsightsData && partnerInsightsData.escalationsError
          )}
        </div>
      </td>
    </tr>`;
}

function renderPartnerInsightsHeaderCells() {
  const sortHeaders = PARTNER_INSIGHTS_SORT_COLUMNS.map(({ key, label }) => {
    const isActive = partnerInsightsSort.key === key;
    const arrow = isActive ? (partnerInsightsSort.dir === "asc" ? " ▲" : " ▼") : "";
    const numClass = key === "name" ? "" : " num";
    return `<th class="sortable-header${numClass}" data-sort-key="${key}">${escapeHtml(label)}${arrow}</th>`;
  }).join("");
  // Not part of `PARTNER_INSIGHTS_SORT_COLUMNS` - the per-partner Update
  // button (see `renderPartnerInsights`) isn't sortable data.
  return sortHeaders + `<th class="num"></th>`;
}

function renderPartnerInsights(data) {
  if (!els.partnerInsightsContainer) return;
  partnerInsightsData = data;
  const partners = partnerInsightsRows();

  const rows = partners
    .map((p) => {
      const bugScore = p.product ? p.product.bugScore : null;
      const isActive = p.partnerId === partnerInsightsActivePartnerId;
      const isUpdating = p.partnerId === partnerInsightsUpdatingId;
      const updateBtn = `<button type="button" class="partner-update-btn" data-partner-id="${escapeHtml(
        p.partnerId
      )}" ${isUpdating ? "disabled" : ""} title="Refresh just ${escapeHtml(p.name)} (Linear score + latest escalations)">${
        isUpdating ? '<span class="spinner"></span>' : "⟳"
      }</button>`;
      const mainRow = `
      <tr class="clickable-row${isActive ? " active-row" : ""}" data-partner-id="${escapeHtml(p.partnerId)}">
        <td>${escapeHtml(p.name)}${!p.matched ? ' <span class="unmatched-flag" title="Couldn\'t be matched between Linear and Intercom">⚠</span>' : ""}</td>
        <td class="num">${renderScoreCell(bugScore, "not linked")}</td>
        <td class="num">${renderEscalationCountCell(p.escalations, data.escalationsConfigured !== false, "LIVE_FIRE", data.escalationsError)}</td>
        <td class="num">${renderEscalationCountCell(p.escalations, data.escalationsConfigured !== false, "SMOLDERING", data.escalationsError)}</td>
        <td class="num">${renderEscalationCountCell(p.escalations, data.escalationsConfigured !== false, "WATCH", data.escalationsError)}</td>
        <td class="num">${updateBtn}</td>
      </tr>`;
      return isActive ? mainRow + renderPartnerInsightsExpandedRow(p) : mainRow;
    })
    .join("");

  els.partnerInsightsContainer.innerHTML = `
    <div class="squad-block">
      <p class="quality-definitions" style="list-style: none; padding-left: 0;">
        Only partners matched to a Vitally account are listed here. Bug score reflects bug-SLA
        responsiveness (100 = clean), from that partner's Linear customer requests. Live Fire,
        Smoldering, and Watch are counts of that partner's currently-tracked escalation items at each
        severity, from the escalation agent's triage of that partner's recent human-written emails and
        Intercom conversations (synced via Vitally). The agent re-checks every couple of hours on
        weekdays; Update here just re-reads what it has saved.${
          data.escalationsConfigured === false
            ? " Escalations aren't configured yet (set ESCALATION_AGENT_URL) - see README."
            : data.escalationsError
              ? " The escalation agent couldn't be reached just now - counts will return when it responds."
              : ""
        }
        Click a partner for the full breakdown, including Feature score and the source
        emails/conversations themselves.
      </p>
      <table class="data-table partner-insights-table">
        <thead>
          <tr>${renderPartnerInsightsHeaderCells()}</tr>
        </thead>
        <tbody>${
          rows ||
          `<tr><td colspan="${PARTNER_INSIGHTS_COLUMNS}"><p class="empty-note">${
            data.vitallyConfigured === false
              ? "No partners found - VITALLY_ACCESS_TOKEN isn't configured, and this tab only shows partners matched to a Vitally account. See README."
              : "No partners matched to a Vitally account found."
          }</p></td></tr>`
        }</tbody>
      </table>
    </div>`;

  if (els.partnerInsightsUpdatedAt && data.fetchedAt) {
    els.partnerInsightsUpdatedAt.textContent = `Updated ${formatRelativeTime(data.fetchedAt)}`;
    els.partnerInsightsUpdatedAt.classList.toggle("stale", isStale(data.fetchedAt));
    els.partnerInsightsUpdatedAt.title = new Date(data.fetchedAt * 1000).toLocaleString();
  }
}

if (els.partnerInsightsContainer) {
  els.partnerInsightsContainer.addEventListener("click", (event) => {
    if (!partnerInsightsData) return;

    const updateBtn = event.target.closest("button.partner-update-btn");
    if (updateBtn) {
      // Stop this from also bubbling into the "row clicked -> toggle
      // expanded state" handler below.
      event.stopPropagation();
      updateSinglePartner(updateBtn.dataset.partnerId);
      return;
    }

    const header = event.target.closest("th.sortable-header");
    if (header) {
      const key = header.dataset.sortKey;
      // Same column clicked again -> flip direction; a different column ->
      // start fresh at ascending.
      partnerInsightsSort.dir = partnerInsightsSort.key === key && partnerInsightsSort.dir === "asc" ? "desc" : "asc";
      partnerInsightsSort.key = key;
      renderPartnerInsights(partnerInsightsData);
      return;
    }

    const findingRow = event.target.closest("tr.escalation-finding-row");
    if (findingRow) {
      const key = findingRow.dataset.escalationKey;
      if (partnerInsightsExpandedEscalations.has(key)) {
        partnerInsightsExpandedEscalations.delete(key);
      } else {
        partnerInsightsExpandedEscalations.add(key);
      }
      renderPartnerInsights(partnerInsightsData);
      return;
    }

    const row = event.target.closest("tr.clickable-row");
    if (!row) return;
    const partnerId = row.dataset.partnerId;
    partnerInsightsActivePartnerId = partnerInsightsActivePartnerId === partnerId ? null : partnerId;
    renderPartnerInsights(partnerInsightsData);
  });
}

let partnerInsightsLoaded = false;

async function loadPartnerInsights() {
  if (!els.partnerInsightsContainer) return;
  try {
    const res = await fetch("/api/partner-insights");
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    renderPartnerInsights(await res.json());
  } catch (err) {
    els.partnerInsightsContainer.innerHTML = `<p class="empty-note">Couldn't load partner insights: ${escapeHtml(
      err.message
    )}</p>`;
  }
}

async function refreshPartnerInsights() {
  const btn = els.partnerInsightsUpdateBtn;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span><span class="btn-label">Updating… (Linear + Intercom + Vitally + LLM, can be slow)</span>';
  }
  try {
    const res = await fetch("/api/partner-insights/refresh", { method: "POST" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    renderPartnerInsights(await res.json());
  } catch (err) {
    showError(`Couldn't update partner insights: ${err.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<span class="btn-label">Update</span>';
    }
  }
}

if (els.partnerInsightsUpdateBtn) {
  els.partnerInsightsUpdateBtn.addEventListener("click", refreshPartnerInsights);
}

// Per-row Update button (⟳ next to each partner) - refreshes just that
// one partner's Product score and re-reads its escalations instead of the
// whole roster (see `refreshPartnerInsights` above and the server-side
// `POST /api/partner-insights/refresh/{partner_id}`).
async function updateSinglePartner(partnerId) {
  if (partnerInsightsUpdatingId) return; // one at a time is enough
  const partner = ((partnerInsightsData && partnerInsightsData.partners) || []).find(
    (p) => p.partnerId === partnerId
  );
  partnerInsightsUpdatingId = partnerId;
  renderPartnerInsights(partnerInsightsData);
  try {
    const res = await fetch(`/api/partner-insights/refresh/${encodeURIComponent(partnerId)}`, { method: "POST" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Request failed (${res.status})`);
    }
    const updated = await res.json();
    const partners = (partnerInsightsData && partnerInsightsData.partners) || [];
    const idx = partners.findIndex((p) => p.partnerId === partnerId);
    if (idx >= 0) {
      partners[idx] = updated;
    } else {
      partners.push(updated);
    }
  } catch (err) {
    showError(`Couldn't update ${partner ? partner.name : partnerId}: ${err.message}`);
  } finally {
    partnerInsightsUpdatingId = null;
    renderPartnerInsights(partnerInsightsData);
  }
}

// ---- Tabs ----

function switchTab(tabName) {
  els.tabButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tabName));
  els.tabPanels.forEach((panel) => panel.classList.toggle("hidden", panel.id !== `tab-${tabName}`));
  if (tabName === "project-milestones" && !milestonesReportLoaded) {
    milestonesReportLoaded = true;
    loadMilestonesReport();
  }
  if (tabName === "support-report" && !supportReportLoaded) {
    supportReportLoaded = true;
    loadSupportReport();
  }
  if (tabName === "partner-insights" && !partnerInsightsLoaded && partnerInsightsAccess) {
    partnerInsightsLoaded = true;
    loadPartnerInsights();
  }
}

els.tabButtons.forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

handleNotionRedirectParams();
loadCurrentUser();
loadNotionStatus();
loadDashboard();
