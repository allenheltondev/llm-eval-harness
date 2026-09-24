import { cloneElement, isValidElement, useId, useState, type ReactNode } from 'react'

/**
 * Hover/focus tooltip. `@readysetcloud/ui` ships no Tooltip, so this stays
 * hand-rolled but uses the design tokens (inverted foreground/background)
 * rather than hardcoded palette colors. It opens on pointer hover and when
 * focus moves inside the wrapper, closes on leave/blur or Escape, and the
 * bubble (role="tooltip") is linked to the trigger element via aria-describedby.
 */
export interface TooltipProps {
  children: ReactNode
  content?: string
  position?: 'top' | 'bottom' | 'left' | 'right'
}

const positionClasses = {
  top: 'bottom-full left-1/2 transform -translate-x-1/2 mb-2',
  bottom: 'top-full left-1/2 transform -translate-x-1/2 mt-2',
  left: 'right-full top-1/2 transform -translate-y-1/2 mr-2',
  right: 'left-full top-1/2 transform -translate-y-1/2 ml-2'
}

const arrowClasses = {
  top: 'top-full left-1/2 transform -translate-x-1/2 border-l-4 border-r-4 border-t-4 border-transparent border-t-foreground',
  bottom:
    'bottom-full left-1/2 transform -translate-x-1/2 border-l-4 border-r-4 border-b-4 border-transparent border-b-foreground',
  left: 'left-full top-1/2 transform -translate-y-1/2 border-t-4 border-b-4 border-l-4 border-transparent border-l-foreground',
  right:
    'right-full top-1/2 transform -translate-y-1/2 border-t-4 border-b-4 border-r-4 border-transparent border-r-foreground'
}

const Tooltip = ({ children, content, position = 'top' }: TooltipProps) => {
  const [isVisible, setIsVisible] = useState(false)
  const tooltipId = useId()
  const open = isVisible && Boolean(content)
  const describedBy = open ? tooltipId : undefined
  const trigger = isValidElement<{ 'aria-describedby'?: string }>(children)
    ? cloneElement(children, {
        'aria-describedby':
          [children.props['aria-describedby'], describedBy].filter(Boolean).join(' ') || undefined
      })
    : children

  return (
    <div
      className="relative inline-block"
      onMouseEnter={() => setIsVisible(true)}
      onMouseLeave={() => setIsVisible(false)}
      onFocus={() => setIsVisible(true)}
      onBlur={event => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setIsVisible(false)
      }}
      onKeyDown={event => {
        if (event.key === 'Escape') setIsVisible(false)
      }}
    >
      {trigger}
      {open && (
        <div className={`absolute z-50 ${positionClasses[position]}`}>
          <div
            id={tooltipId}
            role="tooltip"
            className="bg-foreground text-background text-xs rounded py-2 px-3 w-80 whitespace-pre-line shadow-medium"
          >
            {content}
          </div>
          <div className={`absolute w-0 h-0 ${arrowClasses[position]}`}></div>
        </div>
      )}
    </div>
  )
}

export default Tooltip
