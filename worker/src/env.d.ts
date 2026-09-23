interface Env {
  DATA: R2Bucket;
  QUERY: D1Database;
  PALOMAR_QUERY_UPDATE_TOKEN: string;
  PALOMAR_RETIRE_STATIC_SEARCH?: string;
  PALOMAR_AVAILABILITY_UPDATE_TOKEN: string;
}

declare namespace Cloudflare {
  interface Env {
    PALOMAR_QUERY_UPDATE_TOKEN: string;
    PALOMAR_RETIRE_STATIC_SEARCH?: string;
    PALOMAR_AVAILABILITY_UPDATE_TOKEN: string;
  }

  interface ProductionEnv {
    PALOMAR_QUERY_UPDATE_TOKEN: string;
    PALOMAR_RETIRE_STATIC_SEARCH?: string;
    PALOMAR_AVAILABILITY_UPDATE_TOKEN: string;
  }
}
