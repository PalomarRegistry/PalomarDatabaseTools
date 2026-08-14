import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // Keep the broad fake-binding suite fast and explicit. The separate
    // runtime configuration runs the smaller real-workerd boundary suite.
    include: ["test/*.test.ts"],
  },
});
