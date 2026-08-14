interface Env {
  DATA: R2Bucket;
  PALOMAR_AVAILABILITY_UPDATE_TOKEN: string;
}

declare namespace Cloudflare {
  interface Env {
    PALOMAR_AVAILABILITY_UPDATE_TOKEN: string;
  }

  interface ProductionEnv {
    PALOMAR_AVAILABILITY_UPDATE_TOKEN: string;
  }
}
