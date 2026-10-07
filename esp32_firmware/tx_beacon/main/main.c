/*
 * WiSense-Twin — Section 13: ESP32 Transmitter Firmware (tx_beacon)
 *
 * Emits a strictly periodic, low-jitter stream of WiFi frames at a known,
 * Kconfig-configurable rate and channel via ESP-NOW broadcast.
 *
 * This firmware permanently eliminates timing ambiguity (such as the ~70 Hz
 * 8-12ms jitter and 11.9s stall observed in exploratory ambient captures)
 * by establishing a deterministic, verifiable RF emitter for CSI sensing.
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "nvs_flash.h"

#define TAG "tx_beacon"

#ifndef CONFIG_TX_BEACON_PACKET_RATE
#define CONFIG_TX_BEACON_PACKET_RATE 100
#endif

#ifndef CONFIG_TX_BEACON_WIFI_CHANNEL
#define CONFIG_TX_BEACON_WIFI_CHANNEL 11
#endif

#ifndef CONFIG_TX_BEACON_LOG_INTERVAL_SEC
#define CONFIG_TX_BEACON_LOG_INTERVAL_SEC 1
#endif

#ifndef CONFIG_TX_BEACON_PAYLOAD_SIZE
#define CONFIG_TX_BEACON_PAYLOAD_SIZE 32
#endif

/*
 * Fixed transmitter MAC address: 1a:00:00:00:00:00
 * Matches CONFIG_CSI_SEND_MAC expected by the paired CSI receiver.
 */
static const uint8_t s_tx_mac[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};

/*
 * Broadcast destination MAC address: FF:FF:FF:FF:FF:FF
 */
static const uint8_t s_broadcast_mac[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

/*
 * Packet payload layout.
 * The first 4 bytes are the sequence number, aligning directly with
 * `rx_id = *(uint32_t *)(info->payload + 15)` in the paired csi_recv callback.
 */
#pragma pack(push, 1)
typedef struct {
    uint32_t seq_num;       // Monotonic packet sequence counter (bytes 0-3)
    uint32_t timestamp_ms;  // Sender timestamp in milliseconds since boot
    uint16_t packet_rate;   // Target transmission rate in Hz
    uint8_t  channel;       // Configured WiFi channel
    uint8_t  magic;         // Diagnostic identifier: 'W' (0x57)
} tx_beacon_header_t;
#pragma pack(pop)

/**
 * @brief Initialize Non-Volatile Storage (required for WiFi calibration)
 */
static void init_nvs(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "Erasing NVS flash due to version mismatch or full storage");
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
}

/**
 * @brief Initialize WiFi in Station mode on the configured channel with fixed MAC.
 */
static void init_wifi(void)
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

    // Configure 40 MHz channel bandwidth for dense subcarrier CSI capture
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(ESP_IF_WIFI_STA, WIFI_BW_HT40));
    ESP_ERROR_CHECK(esp_wifi_start());

    // Disable WiFi power-save to ensure deterministic frame timing
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    // Secondary channel allocation for HT40: channel >= 7 uses below, channel < 7 uses above
    wifi_second_chan_t second_chan = (CONFIG_TX_BEACON_WIFI_CHANNEL >= 7)
                                     ? WIFI_SECOND_CHAN_BELOW
                                     : WIFI_SECOND_CHAN_ABOVE;
    ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_TX_BEACON_WIFI_CHANNEL, second_chan));

    // Set fixed transmitter MAC address
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, s_tx_mac));
    ESP_LOGI(TAG, "WiFi STA initialized: channel=%d, MAC=" MACSTR,
             CONFIG_TX_BEACON_WIFI_CHANNEL, MAC2STR(s_tx_mac));
}

/**
 * @brief Initialize ESP-NOW protocol and add broadcast peer.
 */
static void init_esp_now(void)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((const uint8_t *)"pmk1234567890123"));

    esp_now_peer_info_t peer = {
        .channel   = CONFIG_TX_BEACON_WIFI_CHANNEL,
        .ifidx     = WIFI_IF_STA,
        .encrypt   = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));

    esp_now_rate_config_t rate_config = {
        .phymode = WIFI_PHY_MODE_HT40,
        .rate    = WIFI_PHY_RATE_MCS0_LGI,
        .ersu    = false,
        .dcm     = false,
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));

    ESP_LOGI(TAG, "ESP-NOW broadcast peer added on channel %d (HT40 / MCS0_LGI)",
             CONFIG_TX_BEACON_WIFI_CHANNEL);
}

/**
 * @brief FreeRTOS task transmitting frames with hybrid microsecond pacing
 *        and periodic serial health telemetry.
 */
static void tx_beacon_task(void *pvParameters)
{
    const uint32_t target_rate = CONFIG_TX_BEACON_PACKET_RATE;
    const uint32_t log_interval_sec = CONFIG_TX_BEACON_LOG_INTERVAL_SEC;
    const uint32_t payload_len = (CONFIG_TX_BEACON_PAYLOAD_SIZE < sizeof(tx_beacon_header_t))
                                 ? sizeof(tx_beacon_header_t)
                                 : CONFIG_TX_BEACON_PAYLOAD_SIZE;

    uint8_t *payload_buffer = (uint8_t *)calloc(1, payload_len);
    if (!payload_buffer) {
        ESP_LOGE(TAG, "Failed to allocate %lu bytes for TX payload", (unsigned long)payload_len);
        vTaskDelete(NULL);
        return;
    }

    tx_beacon_header_t *hdr = (tx_beacon_header_t *)payload_buffer;
    hdr->packet_rate = (uint16_t)target_rate;
    hdr->channel     = (uint8_t)CONFIG_TX_BEACON_WIFI_CHANNEL;
    hdr->magic       = 0x57; // 'W' for WiSense

    const int64_t target_interval_us = 1000000LL / target_rate;
    int64_t next_tx_time_us = esp_timer_get_time();

    uint32_t seq = 0;
    uint32_t last_log_ms = (uint32_t)(next_tx_time_us / 1000);
    uint32_t last_log_seq = 0;

    ESP_LOGI(TAG, "================ WiSense TX Beacon Starting ================");
    ESP_LOGI(TAG, "Target rate: %lu pps (interval: %lld us) | Channel: %d | Payload: %lu bytes",
             (unsigned long)target_rate, target_interval_us,
             CONFIG_TX_BEACON_WIFI_CHANNEL, (unsigned long)payload_len);

    while (1) {
        int64_t now_us = esp_timer_get_time();
        uint32_t now_ms = (uint32_t)(now_us / 1000);

        hdr->seq_num      = seq;
        hdr->timestamp_ms = now_ms;

        esp_err_t err = esp_now_send(s_broadcast_mac, payload_buffer, payload_len);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "esp_now_send error at seq=%" PRIu32 ": %s", seq, esp_err_to_name(err));
        }

        seq++;

        // Periodic telemetry status log to serial monitor
        if ((now_ms - last_log_ms) >= (log_interval_sec * 1000)) {
            uint32_t elapsed_ms = now_ms - last_log_ms;
            uint32_t pkts_in_window = seq - last_log_seq;
            float actual_rate = (elapsed_ms > 0) ? ((float)pkts_in_window * 1000.0f / (float)elapsed_ms) : 0.0f;

            ESP_LOGI(TAG, "[TX_STAT] count=%" PRIu32 " | window_pkts=%" PRIu32 " | uptime=%" PRIu32 " ms | rate=%.1f pps (target=%" PRIu32 ") | heap=%" PRIu32,
                     seq,
                     pkts_in_window,
                     now_ms,
                     actual_rate,
                     target_rate,
                     (uint32_t)esp_get_free_heap_size());

            last_log_ms = now_ms;
            last_log_seq = seq;
        }

        // Schedule next transmission time with zero cumulative drift
        next_tx_time_us += target_interval_us;
        now_us = esp_timer_get_time();
        int64_t wait_us = next_tx_time_us - now_us;

        // Yield CPU to FreeRTOS idle and background tasks if wait exceeds 2ms
        if (wait_us > 2000) {
            TickType_t delay_ticks = pdMS_TO_TICKS((wait_us - 1000) / 1000);
            if (delay_ticks > 0) {
                vTaskDelay(delay_ticks);
            }
        }

        // Tight microsecond spin for the sub-millisecond tail to ensure zero-jitter emission
        while (esp_timer_get_time() < next_tx_time_us) {
            // tight spin wait
        }
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "Initializing WiSense TX Beacon Firmware...");

    init_nvs();
    init_wifi();
    init_esp_now();

    // Pin transmission task to Core 1 to leave Core 0 dedicated to the WiFi MAC/PHY driver
    BaseType_t ret = xTaskCreatePinnedToCore(
        tx_beacon_task,
        "tx_beacon_task",
        4096,
        NULL,
        5,
        NULL,
        1
    );

    if (ret != pdPASS) {
        ESP_LOGE(TAG, "Failed to create tx_beacon_task");
    }
}
