/**
 * `npm run build` serves `vendor/kirocrew-app-sdk.d.ts`, so a TypeScript app can
 * typecheck its imports of `@kirocrew/app-sdk` and `@kirocrew/app-sdk/ui`. This runs
 * the real generator and compiles an app source against what it wrote.
 */
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import ts from 'typescript'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

const WEBSITE = path.resolve(__dirname, '../..')
// TypeScript hands the compiler host forward-slash paths on every platform.
const APP_DIR = `${WEBSITE.split(path.sep).join('/')}/__app__/src`

let scratch = ''
let declarations = ''

beforeAll(() => {
  scratch = mkdtempSync(path.join(tmpdir(), 'app-sdk-types-'))
  const out = path.join(scratch, 'kirocrew-app-sdk.d.ts')
  // A child process, so the generator's whole-dashboard program is not held in this
  // worker's heap.
  execFileSync(process.execPath, [path.join(WEBSITE, 'scripts/gen-app-sdk-types.mjs'), out], { stdio: 'pipe' })
  declarations = readFileSync(out, 'utf8')
}, 180_000)

afterAll(() => {
  rmSync(scratch, { recursive: true, force: true })
})

/** Type errors of `code` compiled as an app's `src/App.tsx` beside the declarations. */
function typeErrors(code: string): string[] {
  // Both files sit inside website/ so `react` resolves from its node_modules, as it
  // does from an app's own. No skipLibCheck: the declaration file is checked too.
  const files = new Map([
    [`${APP_DIR}/App.tsx`, code],
    [`${APP_DIR}/kirocrew-app-sdk.d.ts`, declarations],
  ])
  const options: ts.CompilerOptions = {
    strict: true,
    noEmit: true,
    jsx: ts.JsxEmit.ReactJSX,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    target: ts.ScriptTarget.ES2020,
    lib: ['lib.es2023.d.ts', 'lib.dom.d.ts', 'lib.dom.iterable.d.ts'],
    types: [],
  }
  const host = ts.createCompilerHost(options)
  const getSourceFile = host.getSourceFile.bind(host)
  const fileExists = host.fileExists.bind(host)
  host.getSourceFile = (f, lang, ...rest) => {
    const text = files.get(f)
    return text === undefined ? getSourceFile(f, lang, ...rest) : ts.createSourceFile(f, text, lang)
  }
  host.fileExists = (f) => files.has(f) || fileExists(f)
  const program = ts.createProgram([...files.keys()], options, host)
  return ts.getPreEmitDiagnostics(program).map((d) => ts.flattenDiagnosticMessageText(d.messageText, '\n'))
}

describe('vendor/kirocrew-app-sdk.d.ts', () => {
  it('typechecks an app using the SDK hooks and the shared UI components', () => {
    const app = `
      import { useAppApi, useAppEvents } from '@kirocrew/app-sdk'
      import { Card, CardTitle, PageHeader } from '@kirocrew/app-sdk/ui'
      export default function App() {
        const api = useAppApi()
        useAppEvents('notification', () => {})
        void api.get<{ ok: boolean }>('/api/status').then((status) => status.ok)
        return <Card><PageHeader title="Status" /><CardTitle>Overview</CardTitle></Card>
      }
    `
    expect(typeErrors(app)).toEqual([])
  }, 60_000)

  it("types a session control's request, its jump, its go, its insert and the open-file hook, a key's open included", () => {
    // An app copies this one file for current typings, so the newer hooks and the
    // `via` a key opens with have to be in it, typed, not any.
    const app = `
      import { useInsertIntoComposer, useOpenFile, useSessionControlGo, useSessionControlJump, useSessionControlRequest } from '@kirocrew/app-sdk'
      export default function App() {
        const req = useSessionControlRequest()
        const jump = useSessionControlJump()
        const go = useSessionControlGo()
        const insertIntoComposer = useInsertIntoComposer()
        const put = insertIntoComposer?.('Q4a agreed')
        // @ts-expect-error the text is a string
        void insertIntoComposer?.(4)
        const notLiteral: boolean = put?.ok === false && (put.code === 'not_literal' || put.code === 'no_gesture')
        // @ts-expect-error not a code the insert answers
        const bogusCode: boolean = put?.ok === false && put.code === 'no_click'
        const openFile = useOpenFile()
        const byKey: boolean = req?.via === 'key'
        const fromAnotherChat: boolean = req?.via === 'control'
        // @ts-expect-error not a way the box opens
        const bogus: boolean = req?.via === 'menu'
        // @ts-expect-error a jump takes the message's ts
        void jump?.({ when: 1 })
        void jump?.({ ts: '1' })
        const went = go?.('chat-4', { label: 'Q4a' })
        // @ts-expect-error a go takes the chat's key
        void go?.(4)
        const refused: boolean = went?.ok === false && went.reason === 'no_click'
        return <p>{String(byKey || fromAnotherChat || bogus || refused || notLiteral || bogusCode || typeof openFile === 'function')}</p>
      }
    `
    expect(typeErrors(app)).toEqual([])
  }, 60_000)

  it('reports a misuse instead of typing the SDK as any', () => {
    const app = `
      import { useAppApi } from '@kirocrew/app-sdk'
      import { PageHeader } from '@kirocrew/app-sdk/ui'
      export default function App() {
        useAppApi().fetchEverything()
        return <PageHeader heading="Status" />
      }
    `
    const errors = typeErrors(app).join('\n')
    expect(errors).toContain("Property 'fetchEverything' does not exist on type 'AppApi'")
    expect(errors).toMatch(/Property 'heading' does not exist/)
  }, 60_000)
})
