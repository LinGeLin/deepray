# Deepray - Losses

## Components
https://www.tensorflow.org/deepray/api_docs/python/dp/losses

## Contribution Guidelines
#### Standard API
In order to conform with the current API standard, all losses
must:
 * Inherit from `keras.losses.Loss`.
 * Register as a keras global object so it can be serialized properly: `@tf.keras.utils.register_keras_serializable(package='Deepray')`

#### Testing Requirements
 * Simple unittests that demonstrate the loss is behaving as expected on
 some set of known inputs and outputs.
 * To run your `tf.functions` in eager mode and graph mode in the tests, 
   you can use the `@pytest.mark.usefixtures("maybe_run_functions_eagerly")` 
   decorator. This will run the tests twice, once normally, and once
   with `tf.config.run_functions_eagerly(True)`.

#### Documentation Requirements
 * Update the [CODEOWNERS file](https://github.com/deepray-AI/deepray/blob/main/.github/CODEOWNERS)

