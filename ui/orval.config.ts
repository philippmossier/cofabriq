import { defineConfig } from "orval";

// The client is generated from the API's schema (`cofabriq openapi`), never
// written by hand: a changed response model fails `tsc` here.
export default defineConfig({
  cofabriq: {
    input: "./openapi.json",
    output: {
      target: "./src/api/generated.ts",
      client: "react-query",
      httpClient: "fetch",
      mode: "single",
      clean: true,
    },
  },
});
