# Lab 2 prompt files

All prompts describe a fixed traffic-camera view of a signalized intersection (the seed frame in
`assets/inputs/site03_median.jpg`). Wording is neutral traffic-operations language (traffic camera,
vehicle, pedestrians in the roadway); no enforcement or identification framing.

## Qwen edit instructions (`edit_examples.json`)

Example instructions for the seed image, listed by the 1.2 cell for inspiration. The notebook takes a free-text
`EDIT_PROMPT`; these are not a menu. The notebook's default instruction is `snowy_morning`.

## I2V scenario presets (`I2V_PROMPT_FILES` in the notebook)

| File | Preset |
|---|---|
| `i2v_normal_traffic.json` | `normal_traffic` (default) |
| `i2v_red_light_running.json` | `red_light_running` |
| `i2v_wrong_way_driving.json` | `wrong_way_driving` |
| `i2v_stalled_vehicle.json` | `stalled_vehicle` |
| `i2v_collision.json` | `collision` |

`i2v_prompt_schema.json` documents the schema; `llm_generated_prompt.json` is the example output of the
optional 2.2.1 step (shown for reference when the step is skipped). The notebook never writes into this
directory: generated prompts go to `assets/outputs/prompts/`.

## Transfer presets (`TRANSFER_PROMPT_FILES`)

| File | Preset |
|---|---|
| `transfer_nighttime_snowy.json` | `nighttime_snowy` (default) |
| `transfer_nighttime_wet.json` | `nighttime_wet` |

`transfer_prompt_schema.json` documents the schema; `llm_generated_transfer_prompt.json` is the example
output of the optional 3.1.1 step.

## Negative prompts (in `assets/`)

- `negative_prompt_i2v.json` - used by the I2V payload (`negative_prompt` inline string). Copy of the
  Cosmos cookbook file `cookbooks/cosmos3/generator/audiovisual/assets/negative_prompts/image2video/neg_prompt.json`.
- `negative_prompt.json` - used by the transfer spec (`negative_prompt_file`). Same cookbook source.
