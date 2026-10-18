// Written by infrastructure/frontend/deploy_frontend.sh with the real addresses.
// None of these is a secret: the API key identifies this application for the
// usage plan, and every route except /health also needs a signed-in person.
window.FINTRUST_CONFIG = {
  apiUrl: "",
  apiKey: "",
  signIn: { domain: "", clientId: "" }
};
