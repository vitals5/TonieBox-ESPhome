#include "i2s_audio_speaker.h"

#ifdef USE_ESP32

#include <driver/gpio.h>
#include <driver/i2s_std.h>

#include "esphome/components/audio/audio.h"
#include "esphome/components/audio/audio_transfer_buffer.h"

#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include "esp_timer.h"
#include "esp_heap_caps.h"

#include <cmath>

namespace esphome::i2s_audio {

static const char *const TAG = "i2s_audio.speaker";

// Software volume control maps the user-facing (0.0, 1.0) range linearly to a dB reduction in
// [-49.0, 0.0] dB; 0.0 is silence.
static constexpr float SOFTWARE_VOLUME_MIN_DB = -49.0f;

// Rate at which the software gain moves toward a new target.
static constexpr uint32_t GAIN_RAMP_MS_PER_DB = 1;

void I2SAudioSpeakerBase::setup() {
  this->event_group_ = xEventGroupCreate();

  if (this->event_group_ == nullptr) {
    ESP_LOGE(TAG, "Event group creation failed");
    this->mark_failed();
    return;
  }

  // Initialize volume control. When audio_dac is configured, this sets the DAC volume and mute state.
  // When no audio_dac is configured, this initializes software volume control.
  this->set_volume(this->volume_);
  this->set_mute_state(this->mute_state_);

  // Until the I2S driver first starts, dout sits in its reset state (often pulled high, or a JTAG
  // function on the ESP32-S3), which keeps a SPDIF optical transmitter lit. Park it low now.
  this->park_dout_pin_();
}

void I2SAudioSpeakerBase::park_dout_pin_() {
  gpio_reset_pin(this->dout_pin_);
  gpio_set_direction(this->dout_pin_, GPIO_MODE_OUTPUT);
  gpio_set_level(this->dout_pin_, 0);
}

void I2SAudioSpeakerBase::dump_config() {
  ESP_LOGCONFIG(TAG,
                "Speaker:\n"
                "  Pin: %d\n"
                "  Buffer duration: %" PRIu32,
                static_cast<int8_t>(this->dout_pin_), this->buffer_duration_ms_);
  if (this->timeout_.has_value()) {
    ESP_LOGCONFIG(TAG, "  Timeout: %" PRIu32 " ms", this->timeout_.value());
  }
}

void I2SAudioSpeakerBase::loop() {
  uint32_t event_group_bits = xEventGroupGetBits(this->event_group_);

  if (event_group_bits & (SpeakerEventGroupBits::COMMAND_STOP | SpeakerEventGroupBits::COMMAND_STOP_GRACEFULLY)) {
    this->start_failures_ = 0;
    this->cancel_timeout("audio-start");
    this->status_clear_error();
  }

  // stop_() cancels older starts. A later start bit must survive cancellation of a pending startup.
  constexpr uint32_t stop_bits = SpeakerEventGroupBits::COMMAND_STOP | SpeakerEventGroupBits::COMMAND_STOP_GRACEFULLY;
  if ((event_group_bits & stop_bits) && this->speaker_task_handle_ == nullptr &&
      (this->state_ == speaker::STATE_STOPPED || this->state_ == speaker::STATE_STARTING)) {
    xEventGroupClearBits(this->event_group_, stop_bits);
    event_group_bits &= ~stop_bits;
    if (this->tx_handle_ != nullptr) {
      this->stop_i2s_driver_();  // A cancelled partial startup must also release its channel.
    }
    this->state_ = speaker::STATE_STOPPED;
  }

  if ((event_group_bits & SpeakerEventGroupBits::COMMAND_START) && (this->state_ == speaker::STATE_STOPPED)) {
    this->state_ = speaker::STATE_STARTING;
    xEventGroupClearBits(this->event_group_, SpeakerEventGroupBits::COMMAND_START);
  }

  // Handle the task's state
  if (event_group_bits & SpeakerEventGroupBits::TASK_STARTING) {
    ESP_LOGD(TAG, "Starting");
    xEventGroupClearBits(this->event_group_, SpeakerEventGroupBits::TASK_STARTING);
  }
  if (event_group_bits & SpeakerEventGroupBits::TASK_RUNNING) {
    ESP_LOGV(TAG, "Started");
    xEventGroupClearBits(this->event_group_, SpeakerEventGroupBits::TASK_RUNNING);
    this->state_ = speaker::STATE_RUNNING;
    this->start_failures_ = 0;
    this->cancel_timeout("audio-start");
#ifdef USE_AUDIO_DAC
    // The task raises TASK_RUNNING only after enabling the channel, so the clocks are already running
    if (this->audio_dac_ != nullptr) {
      this->audio_dac_->on_audio_started();
    }
#endif  // USE_AUDIO_DAC
  }
  if (event_group_bits & SpeakerEventGroupBits::TASK_STOPPING) {
    ESP_LOGV(TAG, "Stopping");
    xEventGroupClearBits(this->event_group_, SpeakerEventGroupBits::TASK_STOPPING);
    this->state_ = speaker::STATE_STOPPING;
  }
  if (event_group_bits & SpeakerEventGroupBits::TASK_STOPPED) {
    if (this->status_has_error()) {
      return;  // A failed cleanup is latched until an explicit stop permits another attempt.
    }
    ESP_LOGD(TAG, "Stopped");

    if (this->speaker_task_handle_ != nullptr) {
      vTaskDelete(this->speaker_task_handle_);
      this->speaker_task_handle_ = nullptr;
    }

    this->stop_i2s_driver_();
    if (this->tx_handle_ != nullptr) {
      // The task is gone. Consume stop commands so they cannot repeatedly clear the cleanup error.
      xEventGroupClearBits(this->event_group_, stop_bits);
      return;  // Retain TASK_STOPPED and ownership; an explicit stop permits cleanup to retry.
    }
    // Leave COMMAND_START untouched: clearing then re-posting it races a newer stop command.
    const EventBits_t bits_before_clear = xEventGroupClearBits(
        this->event_group_, SpeakerEventGroupBits::ALL_BITS & ~SpeakerEventGroupBits::COMMAND_START);
    this->status_clear_error();
    const bool retry_setup = (bits_before_clear & SpeakerEventGroupBits::ERR_ESP_NO_MEM) &&
                             !(bits_before_clear & stop_bits);
    if (bits_before_clear & stop_bits) {
      // A stop can arrive after the loop snapshot, including during driver cleanup.
      this->start_failures_ = 0;
      this->cancel_timeout("audio-start");
    } else if (retry_setup) {
      this->retry_start_("speaker task setup");
    }

    this->on_task_stopped();

    // The loop owns state: a newer stop bit cancels this retry on the next pass.
    this->state_ = retry_setup ? speaker::STATE_STARTING : speaker::STATE_STOPPED;
  }

  // Handle the speaker's state
  switch (this->state_) {
    case speaker::STATE_STARTING:
      if (this->status_has_error()) {
        break;
      }

      // Never clean a channel while its task can still access it.
      if (this->speaker_task_handle_ != nullptr) {
        break;
      }
      // A partial driver/task-creation failure may have retained a channel after failed deletion.
      if (this->tx_handle_ != nullptr) {
        this->stop_i2s_driver_();
        if (this->tx_handle_ != nullptr) {
          break;  // Cleanup error remains latched; no second channel or busy retry loop.
        }
      }

      if (this->start_i2s_driver(this->audio_stream_info_) != ESP_OK) {
        this->retry_start_("driver initialization");
        break;
      }

      // Seed the ramp at the live target so this run adopts it instantly rather than fading to it
      // from wherever the previous run left off. Posted here, not in the task: the ramp's mailbox
      // allows one writer, and that is the main loop.
      this->post_software_gain_(0);
      xTaskCreate(I2SAudioSpeakerBase::speaker_task, "speaker_task", TASK_STACK_SIZE, (void *) this, TASK_PRIORITY,
                  &this->speaker_task_handle_);

      if (this->speaker_task_handle_ == nullptr) {
        this->retry_start_("task creation");
        this->stop_i2s_driver_();  // Stops the driver to return the lock; will be reloaded in next attempt
      }
      break;
    case speaker::STATE_RUNNING:   // Intentional fallthrough
    case speaker::STATE_STOPPING:  // Intentional fallthrough
    case speaker::STATE_STOPPED:
      break;
  }
}

void I2SAudioSpeakerBase::set_volume(float volume) {
  speaker::Speaker::set_volume(volume);
  this->post_software_gain_(this->audio_stream_info_.ms_to_samples(GAIN_RAMP_MS_PER_DB));
}

void I2SAudioSpeakerBase::set_mute_state(bool mute_state) {
  speaker::Speaker::set_mute_state(mute_state);
  this->post_software_gain_(this->audio_stream_info_.ms_to_samples(GAIN_RAMP_MS_PER_DB));
}

void I2SAudioSpeakerBase::post_software_gain_(uint32_t rate_samples) {
#ifdef USE_AUDIO_DAC
  if (this->audio_dac_ != nullptr) {
    return;  // Hardware volume; the ramp stays at unity
  }
#endif  // USE_AUDIO_DAC
  // Software volume control. The ramp treats 0 dB as unity and skips processing there.
  float target_db;
  if (this->is_silent_()) {
    target_db = -INFINITY;
  } else if (this->volume_ >= 1.0f) {
    target_db = 0.0f;
  } else {
    target_db = remap<float, float>(this->volume_, 0.0f, 1.0f, SOFTWARE_VOLUME_MIN_DB, 0.0f);
  }
  this->gain_ramp_.set_target_db_at_rate(target_db, rate_samples);
}

size_t I2SAudioSpeakerBase::play(const uint8_t *data, size_t length, TickType_t ticks_to_wait) {
  if (this->is_failed()) {
    ESP_LOGE(TAG, "Setup failed; cannot play audio");
    return 0;
  }

  if (this->state_ != speaker::STATE_RUNNING && this->state_ != speaker::STATE_STARTING) {
    this->start();
  }

  if (this->state_ != speaker::STATE_RUNNING) {
    // Unable to write data to a running speaker, so delay the max amount of time so it can get ready
    vTaskDelay(ticks_to_wait);
    ticks_to_wait = 0;
  }

  size_t bytes_written = 0;
  if (this->state_ == speaker::STATE_RUNNING) {
    std::shared_ptr<ring_buffer::RingBuffer> temp_ring_buffer = this->audio_ring_buffer_.lock();
    if (temp_ring_buffer != nullptr) {
      // The weak_ptr locks successfully only while the speaker task owns the ring buffer, so it is safe to write
      bytes_written = temp_ring_buffer->write_without_replacement((void *) data, length, ticks_to_wait);
    }
  }

  return bytes_written;
}

bool I2SAudioSpeakerBase::has_buffered_data() const {
  std::shared_ptr<ring_buffer::RingBuffer> temp_ring_buffer = this->audio_ring_buffer_.lock();
  if (temp_ring_buffer != nullptr) {
    return temp_ring_buffer->available() > 0;
  }
  return false;
}

void I2SAudioSpeakerBase::speaker_task(void *params) {
  I2SAudioSpeakerBase *this_speaker = (I2SAudioSpeakerBase *) params;
  this_speaker->run_speaker_task();
}

void I2SAudioSpeakerBase::start() {
  if (!this->is_ready() || this->is_failed())
    return;
  const auto commands = xEventGroupGetBits(this->event_group_);
  if (((this->state_ == speaker::STATE_STARTING) || (this->state_ == speaker::STATE_RUNNING)) &&
      !(commands & (SpeakerEventGroupBits::COMMAND_STOP | SpeakerEventGroupBits::COMMAND_STOP_GRACEFULLY)))
    return;

  xEventGroupSetBits(this->event_group_, SpeakerEventGroupBits::COMMAND_START);
}

void I2SAudioSpeakerBase::stop() { this->stop_(false); }

void I2SAudioSpeakerBase::finish() { this->stop_(true); }

void I2SAudioSpeakerBase::stop_(bool wait_on_empty) {
  if (!this->is_ready() || this->is_failed())
    return;

  // Cancel an older pending start. A start posted after this stop remains queued until cleanup finishes.
  xEventGroupClearBits(this->event_group_, SpeakerEventGroupBits::COMMAND_START);
  if (wait_on_empty) {
    xEventGroupSetBits(this->event_group_, SpeakerEventGroupBits::COMMAND_STOP_GRACEFULLY);
  } else {
    xEventGroupSetBits(this->event_group_, SpeakerEventGroupBits::COMMAND_STOP);
  }
}

esp_err_t I2SAudioSpeakerBase::init_i2s_channel_(const i2s_chan_config_t &chan_cfg, const i2s_std_config_t &std_cfg,
                                                 size_t event_queue_size) {
  esp_err_t err = i2s_new_channel(&chan_cfg, &this->tx_handle_, NULL);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "I2S channel allocation failed: %s", esp_err_to_name(err));
    this->parent_->unlock();
    return err;
  }

  err = i2s_channel_init_std_mode(this->tx_handle_, &std_cfg);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Failed to initialize I2S channel");
    this->stop_i2s_driver_();
    return err;
  }

  if (this->i2s_event_queue_ == nullptr) {
    this->i2s_event_queue_ = xQueueCreate(event_queue_size, sizeof(int64_t));
  } else {
    // Reset queue to clear any stale events from previous task
    xQueueReset(this->i2s_event_queue_);
  }

  // Lockstep records queue. One record per in-flight DMA buffer; sized to match the I2S event queue
  // so a fully-saturated DMA pipeline cannot overflow either side before drain.
  if (this->write_records_queue_ == nullptr) {
    this->write_records_queue_ = xQueueCreate(event_queue_size, sizeof(uint32_t));
  } else {
    xQueueReset(this->write_records_queue_);
  }

  if (this->i2s_event_queue_ == nullptr || this->write_records_queue_ == nullptr) {
    ESP_LOGE(TAG, "Failed to allocate I2S event queue(s)");
    this->stop_i2s_driver_();
    return ESP_ERR_NO_MEM;
  }

  return ESP_OK;
}

void I2SAudioSpeakerBase::retry_start_(const char *stage) {
  ++this->start_failures_;
  ESP_LOGE(TAG, "%s failed (attempt %u/4): DMA free=%u largest=%u; internal free=%u largest=%u", stage,
           this->start_failures_, (unsigned) heap_caps_get_free_size(MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL),
           (unsigned) heap_caps_get_largest_free_block(MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL),
           (unsigned) heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           (unsigned) heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
  if (this->start_failures_ >= 4) {
    this->cancel_timeout("audio-start");
    ESP_LOGE(TAG, "Start retries exhausted; send stop before trying playback again");
    this->status_set_error(LOG_STR("Audio start retries exhausted"));
  } else {
    this->status_momentary_error("audio-start", 250U << (this->start_failures_ - 1));
  }
}

esp_err_t I2SAudioSpeakerBase::enable_i2s_channel_() {
  const esp_err_t err = i2s_channel_enable(this->tx_handle_);
  this->channel_enabled_ = err == ESP_OK;
  return err;
}

void I2SAudioSpeakerBase::disable_i2s_channel_() {
  if (this->channel_enabled_) {
    const esp_err_t err = i2s_channel_disable(this->tx_handle_);
    if (err == ESP_OK) {
      this->channel_enabled_ = false;
    } else {
      ESP_LOGE(TAG, "I2S disable failed: %s", esp_err_to_name(err));
    }
  }
}

void I2SAudioSpeakerBase::stop_i2s_driver_() {
  if (this->tx_handle_ != nullptr) {
    this->disable_i2s_channel_();
    const esp_err_t err = i2s_del_channel(this->tx_handle_);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "I2S delete failed: %s", esp_err_to_name(err));
      this->status_set_error(LOG_STR("I2S cleanup failed"));
      return;  // Keep the handle and bus ownership; never create a second channel over it.
    }
    this->tx_handle_ = nullptr;
    this->channel_enabled_ = false;

    // i2s_del_channel() leaves dout wired to this port's data-out signal in the GPIO matrix: it only
    // clears an internal reservation mask, never the esp_rom_gpio_connect_out_signal() routing that
    // setup installed. If another speaker reuses this port (shared bus), its audio still reaches our
    // dout. Detach the pin and drive it low so a stale output stops driving downstream hardware: a
    // SPDIF optical transmitter would otherwise stay lit, and an analog DAC would emit noise.
    this->park_dout_pin_();
  }
  if (this->i2s_event_queue_ != nullptr) {
    vQueueDelete(this->i2s_event_queue_);
    this->i2s_event_queue_ = nullptr;
  }
  if (this->write_records_queue_ != nullptr) {
    vQueueDelete(this->write_records_queue_);
    this->write_records_queue_ = nullptr;
  }
  this->parent_->unlock();
}

bool IRAM_ATTR I2SAudioSpeakerBase::i2s_on_sent_cb(i2s_chan_handle_t handle, i2s_event_data_t *event, void *user_ctx) {
  int64_t now = esp_timer_get_time();

  BaseType_t need_yield1 = pdFALSE;
  BaseType_t need_yield2 = pdFALSE;
  BaseType_t need_yield3 = pdFALSE;

  I2SAudioSpeakerBase *this_speaker = (I2SAudioSpeakerBase *) user_ctx;

  if (xQueueIsQueueFullFromISR(this_speaker->i2s_event_queue_)) {
    // Queue is full, so discard the oldest event. The lockstep queues are now desynced; the task resyncs them.
    int64_t dummy;
    xQueueReceiveFromISR(this_speaker->i2s_event_queue_, &dummy, &need_yield1);
    xEventGroupSetBitsFromISR(this_speaker->event_group_, SpeakerEventGroupBits::ERR_DROPPED_EVENT, &need_yield2);
  }

  xQueueSendToBackFromISR(this_speaker->i2s_event_queue_, &now, &need_yield3);

  return need_yield1 | need_yield2 | need_yield3;
}

void I2SAudioSpeakerBase::drain_lockstep_(uint32_t extra_frames) {
  // Stop DMA so no more completion events arrive while the queues are rebuilt
  this->disable_i2s_channel_();
  xEventGroupClearBits(this->event_group_, SpeakerEventGroupBits::ERR_DROPPED_EVENT);

  uint32_t frames = extra_frames;
  uint32_t record_frames = 0;
  while (xQueueReceive(this->write_records_queue_, &record_frames, 0) == pdTRUE) {
    frames += record_frames;
  }
  xQueueReset(this->i2s_event_queue_);

  if (frames > 0) {
    ESP_LOGV(TAG, "Crediting %" PRIu32 " dropped frames as played", frames);
    this->audio_output_callback_(frames, esp_timer_get_time());
  }
}

void I2SAudioSpeakerBase::apply_software_volume_(uint8_t *data, size_t bytes_read) {
#ifdef USE_AUDIO_DAC
  if (this->audio_dac_ != nullptr) {
    return;  // Hardware volume; the ramp is never targeted
  }
#endif  // USE_AUDIO_DAC
  const size_t bytes_per_sample = this->current_stream_info_.samples_to_bytes(1);
  this->gain_ramp_.process(data, static_cast<uint8_t>(bytes_per_sample),
                           this->current_stream_info_.bytes_to_samples(bytes_read));
}

void I2SAudioSpeakerBase::swap_esp32_mono_samples_(uint8_t *data, size_t bytes_read) {
#ifdef USE_ESP32_VARIANT_ESP32
  // For ESP32 16-bit mono mode, adjacent samples need to be swapped.
  if (this->output_stream_info_.get_channels() == 1 && this->output_stream_info_.get_bits_per_sample() == 16) {
    int16_t *samples = reinterpret_cast<int16_t *>(data);
    size_t sample_count = bytes_read / sizeof(int16_t);
    for (size_t i = 0; i + 1 < sample_count; i += 2) {
      int16_t tmp = samples[i];
      samples[i] = samples[i + 1];
      samples[i + 1] = tmp;
    }
  }
#endif  // USE_ESP32_VARIANT_ESP32
}

}  // namespace esphome::i2s_audio

#endif  // USE_ESP32
