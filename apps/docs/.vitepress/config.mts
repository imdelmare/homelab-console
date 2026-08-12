import { defineConfig } from "vitepress";
import { fileURLToPath } from "node:url";

const base = process.env.DOCS_BASE || "/docs/";
const repository = "https://github.com/imdelmare/homelab-console";

export default defineConfig({
  title: "Homelab Console",
  description: "Run your homelab privately with a small local AI.",
  base,
  srcDir: "content",
  vite: {
    // The generated Markdown tree is disposable, while curated screenshots
    // remain versioned under apps/docs/public.
    publicDir: fileURLToPath(new URL("../public", import.meta.url)),
  },
  cleanUrls: true,
  lastUpdated: true,
  head: [
    ["meta", { name: "theme-color", content: "#f1eee5" }],
    ["meta", { property: "og:type", content: "website" }],
    ["meta", { property: "og:title", content: "Homelab Console" }],
  ],
  markdown: {
    lineNumbers: true,
    theme: { light: "github-light", dark: "github-dark" },
  },
  themeConfig: {
    siteTitle: "Homelab Console",
    nav: [
      { text: "Start", link: "/getting-started" },
      { text: "Local AI", link: "/conversation" },
      { text: "Product Tour", link: "/product-tour" },
      { text: "Providers", link: "/providers" },
      { text: "Internals", link: "/architecture" },
      { text: "GitHub", link: repository },
    ],
    sidebar: [
      {
        text: "Start here",
        items: [
          { text: "What is Homelab Console?", link: "/" },
          { text: "Getting started", link: "/getting-started" },
          { text: "Product tour", link: "/product-tour" },
        ],
      },
      {
        text: "Using Homelab Console",
        items: [
          { text: "Local models", link: "/conversation" },
          { text: "Provider setup", link: "/providers" },
          { text: "Approvals and security", link: "/security" },
          { text: "Watchers", link: "/watchers" },
          { text: "Notifications", link: "/notifications" },
          { text: "AI delivery metrics", link: "/metrics" },
        ],
      },
      {
        text: "External agents",
        items: [
          { text: "MCP adapter", link: "/mcp" },
        ],
      },
      {
        text: "Internals",
        items: [
          { text: "Architecture", link: "/architecture" },
          { text: "Security model", link: "/security" },
          { text: "Authentication", link: "/authentication" },
          { text: "External Sentinel", link: "/sentinel" },
        ],
      },
    ],
    socialLinks: [{ icon: "github", link: repository }],
    search: { provider: "local" },
    outline: { level: [2, 3], label: "On this page" },
    lastUpdated: { text: "Updated" },
    footer: {
      copyright: "Homelab Console",
    },
  },
});
