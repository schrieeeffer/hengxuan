(() => {
  document.querySelectorAll("[data-copy-value]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(button.dataset.copyValue || "");
        const original = button.textContent;
        button.textContent = "已复制";
        window.setTimeout(() => { button.textContent = original; }, 1600);
      } catch (_error) {
        const input = button.closest(".copy-field")?.querySelector("input");
        input?.select();
      }
    });
  });
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm || "确定继续吗？")) event.preventDefault();
    });
  });
  document.querySelectorAll("[data-score-scheme]").forEach((select) => {
    const fields = select.closest("form")?.querySelector("[data-custom-score-fields]");
    if (!fields) return;
    const syncCustomFields = () => { fields.hidden = select.value !== "custom"; };
    select.addEventListener("change", syncCustomFields);
    syncCustomFields();
  });

  const scoringRoot = document.querySelector("[data-scoring]");
  if (!scoringRoot) return;

  const roomId = Number(scoringRoot.dataset.roomId);
  let scoringEnabled = scoringRoot.dataset.canScore === "true";
  let rosterRevision = Number(scoringRoot.dataset.rosterRevision || 0);
  let roomStatus = scoringRoot.dataset.roomStatus || "";
  let activeRequests = 0;
  let reloadPending = false;
  let accessLost = false;
  let pollTimer;
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const rankingBody = document.querySelector("[data-ranking-body]");
  const judgeList = document.querySelector("[data-judge-list]");
  const toast = document.querySelector("[data-toast]");
  const toastMark = toast?.querySelector("[data-toast-mark]");
  const toastMessage = toast?.querySelector("[data-toast-message]");
  const globalStatus = document.querySelector("[data-global-status]");
  const resultsTable = document.querySelector("[data-results-table]");
  const resultsLocked = document.querySelector("[data-results-locked]");
  let toastTimer;

  const showToast = (message, kind = "success") => {
    if (!toast) return;
    if (toastMessage) toastMessage.textContent = message;
    else toast.textContent = message;
    if (toastMark) toastMark.textContent = kind === "error" ? "!" : (kind === "info" ? "i" : "✓");
    toast.dataset.kind = kind;
    toast.classList.add("is-visible");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 2300);
  };

  const makeElement = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };

  const hasDirtyInput = () => [...scoringRoot.querySelectorAll(".numeric-score-input")]
    .some((input) => input.value !== (input.dataset.savedValue || ""));

  const requestSafeReload = () => {
    if (activeRequests > 0 || hasDirtyInput()) {
      if (!reloadPending) {
        reloadPending = true;
        if (globalStatus) globalStatus.textContent = "名单有更新，保存当前输入后刷新";
        showToast("候选人或房间状态已更新，当前输入不会被自动清除", "info");
      }
      return;
    }
    window.location.reload();
  };

  const finishRequest = () => {
    activeRequests = Math.max(0, activeRequests - 1);
    if (reloadPending && activeRequests === 0 && !hasDirtyInput()) requestSafeReload();
  };

  const loseRoomAccess = (message = "你的房间权限已变更") => {
    accessLost = true;
    scoringEnabled = false;
    if (pollTimer) window.clearInterval(pollTimer);
    scoringRoot.querySelectorAll("[data-candidate-id]").forEach((row) => setRowDisabled(row, true));
    if (globalStatus) globalStatus.textContent = message;
    showToast(`${message}，请返回房间列表`, "error");
  };

  const responseLostAccess = (response) => response.status === 401
    || response.status === 403
    || response.status === 404
    || (response.redirected && response.url.includes("/login"));

  const renderRanking = (rankings, resultsVisible) => {
    if (!rankingBody) return;
    const fragment = document.createDocumentFragment();
    if (!resultsVisible) {
      rankingBody.replaceChildren(fragment);
      return;
    }
    if (!rankings.length) {
      const row = document.createElement("tr");
      row.className = "empty-ranking-row";
      const cell = makeElement("td", "", "暂无候选人");
      cell.colSpan = 4;
      row.append(cell);
      fragment.append(row);
    }
    rankings.forEach((item) => {
      const complete = item.expected_count > 0 && item.score_count === item.expected_count;
      const row = document.createElement("tr");
      row.className = complete ? "is-complete" : "is-incomplete";
      const rankCell = document.createElement("td");
      rankCell.className = "ranking-position";
      const rankClass = item.rank && item.rank <= 3 ? "rank-badge top-rank" : "rank-badge";
      rankCell.append(makeElement("span", rankClass, item.rank ?? "—"));

      const nameCell = document.createElement("td");
      nameCell.className = "ranking-person";
      nameCell.append(makeElement("strong", "", item.name));
      nameCell.append(
        makeElement("small", "", item.candidate_no || `第 ${item.position} 位`),
      );

      const averageCell = document.createElement("td");
      averageCell.className = "ranking-average";
      averageCell.append(
        makeElement(
          "strong",
          "average-score",
          item.avg_score === null ? "—" : Number(item.avg_score).toFixed(2),
        ),
      );

      const progressCell = document.createElement("td");
      progressCell.className = "ranking-progress-cell";
      const progressWrap = makeElement("div", "ranking-progress");
      progressWrap.append(makeElement(
        "span",
        complete ? "completion-count complete" : "completion-count incomplete",
        `${item.score_count}/${item.expected_count}`,
      ));
      const progress = document.createElement("progress");
      progress.max = Math.max(item.expected_count, 1);
      progress.value = item.score_count;
      progress.setAttribute("aria-label", `${item.name}的评分进度`);
      progressWrap.append(progress);
      progressCell.append(progressWrap);
      row.append(rankCell, nameCell, averageCell, progressCell);
      fragment.append(row);
    });
    rankingBody.replaceChildren(fragment);
  };

  const renderJudges = (judges) => {
    if (!judgeList) return;
    const fragment = document.createDocumentFragment();
    judges.forEach((judge) => {
      const row = makeElement("div", "judge-row");
      const identity = document.createElement("div");
      identity.append(makeElement("span", "avatar", judge.username.slice(0, 1).toUpperCase()));
      identity.append(makeElement("strong", "", judge.username));
      if (judge.is_current) identity.append(makeElement("small", "", "我"));
      if (judge.is_owner) {
        identity.append(makeElement("small", "", "房主"));
      } else if (judge.role === "admin") {
        identity.append(makeElement("small", "", "协管员"));
      }

      const progressWrap = document.createElement("div");
      progressWrap.append(makeElement("b", "", `${judge.score_count}/${judge.assigned_count}`));
      const progress = document.createElement("progress");
      progress.max = Math.max(judge.assigned_count, 1);
      progress.value = judge.score_count;
      progressWrap.append(progress);
      row.append(identity, progressWrap);
      fragment.append(row);
    });
    judgeList.replaceChildren(fragment);
  };

  const renderState = (state) => {
    if ((rosterRevision && state.roster_revision !== rosterRevision)
        || (roomStatus && state.room_status !== roomStatus)) {
      requestSafeReload();
      return;
    }
    rosterRevision = state.roster_revision;
    roomStatus = state.room_status;
    scoringEnabled = state.can_score === true;
    if (resultsTable) resultsTable.hidden = !state.results_visible;
    if (resultsLocked) resultsLocked.hidden = state.results_visible;
    renderRanking(state.rankings, state.results_visible);
    renderJudges(state.judges);
    const currentScored = document.querySelector("[data-current-scored]");
    const currentAssigned = document.querySelector("[data-current-assigned]");
    const currentProgress = document.querySelector("[data-current-progress]");
    const judgeCount = document.querySelector("[data-judge-count]");
    const totalScores = document.querySelector("[data-total-scores]");
    const totalPossible = document.querySelector("[data-total-possible]");
    if (currentScored) currentScored.textContent = state.current_scored;
    if (currentAssigned) currentAssigned.textContent = state.current_assigned;
    if (currentProgress) {
      currentProgress.max = Math.max(state.current_assigned, 1);
      currentProgress.value = state.current_scored;
    }
    if (judgeCount) judgeCount.textContent = state.judge_count;
    if (totalScores) totalScores.textContent = state.total_scores;
    if (totalPossible) totalPossible.textContent = state.total_possible_scores;
    scoringRoot.querySelectorAll("[data-candidate-id]").forEach((row) => {
      setRowDisabled(row, !scoringEnabled);
    });
  };

  const setRowDisabled = (row, disabled) => {
    const shouldDisable = disabled || row.dataset.assigned !== "true";
    row.querySelectorAll(".score-button, .numeric-score-input, .numeric-save-button, .undo-button")
      .forEach((control) => { control.disabled = shouldDisable; });
  };

  const saveScore = async (row, score, clickedButton = null) => {
    const scoreMin = Number(scoringRoot.querySelector(".numeric-score-input")?.min ?? score);
    const scoreMax = Number(scoringRoot.querySelector(".numeric-score-input")?.max ?? score);
    const numericInput = row.querySelector(".numeric-score-input");
    const scoreButtons = [...row.querySelectorAll(".score-button")];
    const previousButton = scoreButtons.find((item) => item.classList.contains("is-selected"));
    const previousNumericValue = numericInput?.dataset.savedValue ?? numericInput?.value ?? "";
    const status = row.querySelector(".row-status");
    const undoButton = row.querySelector(".undo-button");
    const candidateId = Number(row.dataset.candidateId);

    if (numericInput && numericInput.value.trim() === "") {
      showToast("请先输入分数", "error");
      return;
    }
    if (!Number.isInteger(score) || score < scoreMin || score > scoreMax) {
      showToast(`请输入 ${scoreMin}–${scoreMax} 之间的整数`, "error");
      return;
    }

    activeRequests += 1;
    if (clickedButton) {
      scoreButtons.forEach((button) => {
        const selected = button === clickedButton;
        button.classList.toggle("is-selected", selected);
        button.setAttribute("aria-pressed", selected ? "true" : "false");
      });
    }
    setRowDisabled(row, true);
    if (status) status.textContent = "保存中…";
    if (globalStatus) globalStatus.textContent = "正在保存…";

    try {
      const response = await fetch(`/api/rooms/${roomId}/scores`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ candidate_id: candidateId, score }),
      });
      if (responseLostAccess(response)) {
        loseRoomAccess();
        throw new Error("房间权限已失效");
      }
      const payload = await response.json().catch(() => ({
        error: "请求失败，请刷新页面后重试。",
      }));
      if (!response.ok) throw new Error(payload.error || "保存失败");
      if (numericInput) {
        numericInput.value = String(score);
        numericInput.dataset.savedValue = String(score);
      }
      row.classList.add("is-complete");
      if (undoButton) undoButton.hidden = false;
      if (status) status.textContent = "已保存";
      if (globalStatus) globalStatus.textContent = "全部更改已保存";
      renderState(payload);
      showToast(`已保存 ${score} 分`);
    } catch (error) {
      scoreButtons.forEach((button) => {
        const selected = button === previousButton;
        button.classList.toggle("is-selected", selected);
        button.setAttribute("aria-pressed", selected ? "true" : "false");
      });
      if (numericInput) numericInput.value = previousNumericValue;
      if (status) status.textContent = previousButton || previousNumericValue ? "已保存" : "待评分";
      if (!accessLost) {
        if (globalStatus) globalStatus.textContent = "保存失败，请重试";
        showToast(error.message || "保存失败，请重试", "error");
      }
    } finally {
      setRowDisabled(row, !scoringEnabled);
      finishRequest();
    }
  };

  const removeScore = async (button) => {
    const row = button.closest("[data-candidate-id]");
    if (!row) return;
    const candidateName = row.querySelector(".candidate-identity h3")?.textContent || "这位候选人";
    if (!window.confirm(`确定撤销对“${candidateName}”的这条评分吗？`)) return;
    const candidateId = Number(row.dataset.candidateId);
    const status = row.querySelector(".row-status");
    activeRequests += 1;
    setRowDisabled(row, true);
    if (status) status.textContent = "撤销中…";
    if (globalStatus) globalStatus.textContent = "正在撤销…";

    try {
      const response = await fetch(`/api/rooms/${roomId}/scores/${candidateId}`, {
        method: "DELETE",
        headers: { "X-CSRF-Token": csrfToken, Accept: "application/json" },
      });
      if (responseLostAccess(response)) {
        loseRoomAccess();
        throw new Error("房间权限已失效");
      }
      const payload = await response.json().catch(() => ({
        error: "请求失败，请刷新页面后重试。",
      }));
      if (!response.ok) throw new Error(payload.error || "撤销失败");
      row.querySelectorAll(".score-button").forEach((scoreButton) => {
        scoreButton.classList.remove("is-selected");
        scoreButton.setAttribute("aria-pressed", "false");
      });
      const numericInput = row.querySelector(".numeric-score-input");
      if (numericInput) {
        numericInput.value = "";
        numericInput.dataset.savedValue = "";
      }
      row.classList.remove("is-complete");
      button.hidden = true;
      if (status) status.textContent = "待评分";
      if (globalStatus) globalStatus.textContent = "全部更改已保存";
      renderState(payload);
      showToast("评分已撤销");
    } catch (error) {
      if (status) status.textContent = "已保存";
      if (!accessLost) {
        if (globalStatus) globalStatus.textContent = "撤销失败，请重试";
        showToast(error.message || "撤销失败，请重试", "error");
      }
    } finally {
      setRowDisabled(row, !scoringEnabled);
      finishRequest();
    }
  };

  scoringRoot.querySelectorAll(".numeric-score-input").forEach((input) => {
    input.dataset.savedValue = input.value;
    input.addEventListener("input", () => {
      if (globalStatus && hasDirtyInput()) globalStatus.textContent = "有尚未保存的输入";
      else if (globalStatus) globalStatus.textContent = "全部更改已保存";
    });
  });

  scoringRoot.addEventListener("click", (event) => {
    const undoButton = event.target.closest(".undo-button");
    if (undoButton && !undoButton.disabled) {
      removeScore(undoButton);
      return;
    }
    const scoreButton = event.target.closest(".score-button");
    if (scoreButton && !scoreButton.disabled) {
      const row = scoreButton.closest("[data-candidate-id]");
      saveScore(row, Number(scoreButton.dataset.score), scoreButton);
      return;
    }
    const numericSave = event.target.closest(".numeric-save-button");
    if (numericSave && !numericSave.disabled) {
      const row = numericSave.closest("[data-candidate-id]");
      const input = row.querySelector(".numeric-score-input");
      saveScore(row, Number(input.value));
    }
  });

  scoringRoot.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !event.target.matches(".numeric-score-input")) return;
    event.preventDefault();
    const row = event.target.closest("[data-candidate-id]");
    saveScore(row, Number(event.target.value));
  });

  const refreshState = async () => {
    if (document.hidden || accessLost) return;
    try {
      const response = await fetch(`/api/rooms/${roomId}/rankings`, {
        headers: { Accept: "application/json" },
      });
      if (responseLostAccess(response)) {
        loseRoomAccess();
        return;
      }
      if (response.ok) renderState(await response.json());
    } catch (_error) {
      // A later refresh retries without disturbing the current scoring controls.
    }
  };

  pollTimer = window.setInterval(refreshState, 4000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshState();
  });
})();
