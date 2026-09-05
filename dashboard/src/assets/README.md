# Dashboard image assets

Brand marks imported by the Vue components (bundled by Vite at build time).

- `aws-logo.png`: the AWS "Smile" logo. Shown ~18px tall, left of the
  "Create the read-only role in AWS" button on the setup and settings screens
  (`components/SetupScreen.vue`). Used as-is: no recolor, no stretch.
- `github-mark.png`: the GitHub Invertocat mark. Shown ~18px tall, left of the
  "Sign in with GitHub" button on the login page (`components/LoginScreen.vue`).

Drop the two PNGs here with exactly these names. The dashboard build
(`npm run build`) resolves them via `import ... from '../assets/<name>.png'`, so
both files must be present for the build to succeed.
