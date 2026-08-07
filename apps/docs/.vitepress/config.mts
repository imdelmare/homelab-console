import { defineConfig } from "vitepress";

const base = process.env.DOCS_BASE || "/docs/";
const repository = "https://github.com/imdelmare/homelab-mcp";

export default defineConfig({
  title: "Homelab Console",
  description: "Documentation for the AI-native homelab control plane.",
  base,
  srcDir: "content",
  cleanUrls: true,
  lastUpdated: true,
  head: [
    ["meta", { name: "theme-color", content: "#f45d22" }],
    ["meta", { property: "og:type", content: "website" }],
    ["meta", { property: "og:title", content: "Homelab Console Field Manual" }],
  ],
  markdown: {
    lineNumbers: true,
    theme: { light: "github-light", dark: "github-dark" },
  },
  themeConfig: {
    siteTitle: "HC / Field Manual",
    nav: [
      { text: "Start", link: "/getting-started" },
      { text: "Architecture", link: "/architecture" },
      { text: "MCP Clients", link: "/mcp" },
      { text: "Security", link: "/security" },
      { text: "GitHub", link: repository },
    ],
    sidebar: [
      {
        text: "Orientation",
        items: [
          { text: "Field manual", link: "/" },
          { text: "Getting started", link: "/getting-started" },
        ],
      },
      {
        text: "Core system",
        items: [
          { text: "Architecture", link: "/architecture" },
          { text: "Security model", link: "/security" },
          { text: "Authentication", link: "/authentication" },
        ],
      },
      {
        text: "Agents and automation",
        items: [
          { text: "MCP adapter", link: "/mcp" },
          { text: "Conversation service", link: "/conversation" },
          { text: "Watchers", link: "/watchers" },
          { text: "Notifications", link: "/notifications" },
          { text: "AI metrics", link: "/metrics" },
        ],
      },
      {
        text: "Infrastructure",
        items: [
          { text: "Provider contracts", link: "/providers" },
          { text: "External Sentinel", link: "/sentinel" },
        ],
      },
    ],
    socialLinks: [{ icon: "github", link: repository }],
    search: { provider: "local" },
    outline: { level: [2, 3], label: "On this page" },
    lastUpdated: { text: "Updated" },
    footer: {
      message: "Typed tools. Explicit trust. Human authority.",
      copyright: "Homelab Console documentation",
    },
  },
});
