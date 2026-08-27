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

async function loadCurrentUser() {
  try {
    const res = await fetch("/api/me");
    const info = await res.json();
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

// ---- Tabs ----

function switchTab(tabName) {
  els.tabButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tabName));
  els.tabPanels.forEach((panel) => panel.classList.toggle("hidden", panel.id !== `tab-${tabName}`));
  if (tabName === "project-milestones" && !milestonesReportLoaded) {
    milestonesReportLoaded = true;
    loadMilestonesReport();
  }
}

els.tabButtons.forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

handleNotionRedirectParams();
loadCurrentUser();
loadNotionStatus();
loadDashboard();
