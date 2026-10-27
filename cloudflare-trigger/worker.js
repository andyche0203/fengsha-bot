const OWNER = "andyche0203";
const REPO = "fengsha-bot";
const WORKFLOW = "bot.yml";
const REF = "main";

export default {
  async fetch() {
    return new Response("Fengsha broadcast scheduler is healthy.", {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },

  async scheduled(controller, env) {
    if (!env.GITHUB_TOKEN) {
      throw new Error("Missing GITHUB_TOKEN secret");
    }

    const endpoint = "https://api.github.com/repos/" + OWNER + "/" + REPO +
      "/actions/workflows/" + WORKFLOW + "/dispatches";
    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + env.GITHUB_TOKEN,
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "fengsha-scheduled-trigger",
      },
      body: JSON.stringify({ ref: REF }),
    });

    if (response.status !== 204) {
      const detail = (await response.text()).slice(0, 500);
      throw new Error("GitHub workflow dispatch failed: HTTP " + response.status + " " + detail);
    }

    console.log("Dispatched " + OWNER + "/" + REPO + " workflow on " + REF);
  },
};
